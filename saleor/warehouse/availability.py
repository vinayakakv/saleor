from collections import defaultdict
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, NoReturn, Optional, Tuple

from django.core.exceptions import ValidationError
from django.db.models import F, Sum
from django.db.models.functions import Coalesce

from ..checkout.error_codes import CheckoutErrorCode
from ..core.exceptions import InsufficientStock, InsufficientStockData
from ..product.models import ProductVariantChannelListing
from .models import Reservation, Stock, StockQuerySet

if TYPE_CHECKING:
    from ..checkout.fetch import CheckoutLineInfo
    from ..checkout.models import CheckoutLine
    from ..product.models import Product, ProductVariant


def _get_available_quantity(
    stocks: StockQuerySet,
    checkout_lines: Optional[List["CheckoutLine"]] = None,
    check_reservations: bool = False,
) -> int:
    results = stocks.aggregate(
        total_quantity=Coalesce(Sum("quantity", distinct=True), 0),
        quantity_allocated=Coalesce(Sum("allocations__quantity_allocated"), 0),
    )
    total_quantity = results["total_quantity"]
    quantity_allocated = results["quantity_allocated"]

    if check_reservations:
        quantity_reserved = get_reserved_quantity(stocks, checkout_lines)
    else:
        quantity_reserved = 0

    return max(total_quantity - quantity_allocated - quantity_reserved, 0)


def check_stock_and_preorder_quantity(
    variant: "ProductVariant", country_code: str, channel_slug: str, quantity: int
):
    """Validate if there is stock/preorder available for given variant.

    :raises InsufficientStock: when there is not enough items in stock for a variant
    or there is not enough available preorder items for a variant.
    """
    if variant.is_preorder_active():
        check_preorder_threshold(variant, quantity, channel_slug)
    else:
        check_stock_quantity(variant, country_code, channel_slug, quantity)


def check_stock_quantity(
    variant: "ProductVariant",
    country_code: str,
    channel_slug: str,
    quantity: int,
    checkout_lines: Optional[List["CheckoutLine"]] = None,
    check_reservations: bool = False,
):
    """Validate if there is stock available for given variant in given country.

    If so - returns None. If there is less stock then required raise InsufficientStock
    exception.
    """
    if variant.track_inventory:
        stocks = Stock.objects.get_variant_stocks_for_country(
            country_code, channel_slug, variant
        )
        if not stocks:
            raise InsufficientStock([InsufficientStockData(variant=variant)])

        available_quantity = _get_available_quantity(
            stocks, checkout_lines, check_reservations
        )
        if quantity > available_quantity:
            raise InsufficientStock([InsufficientStockData(variant=variant)])


def check_stock_and_preorder_quantity_bulk(
    variants: Iterable["ProductVariant"],
    country_code: str,
    quantities: Iterable[int],
    channel_slug: str,
    global_quantity_limit: Optional[int],
    additional_filter_lookup: Optional[Dict[str, Any]] = None,
    existing_lines: Iterable = None,
    replace: bool = False,
    check_reservations: bool = False,
):
    """Validate if products are available for stocks/preorder.

    :raises InsufficientStock: when there is not enough items in stock for a variant
    or there is not enough available preorder items for a variant.
    """
    (
        stock_variants,
        stock_quantities,
        preorder_variants,
        preorder_quantities,
    ) = _split_lines_for_trackable_and_preorder(variants, quantities)
    if stock_variants:
        check_stock_quantity_bulk(
            stock_variants,
            country_code,
            stock_quantities,
            channel_slug,
            global_quantity_limit,
            additional_filter_lookup,
            existing_lines,
            replace,
            check_reservations,
        )
    if preorder_variants:
        check_preorder_threshold_bulk(
            preorder_variants,
            preorder_quantities,
            channel_slug,
            global_quantity_limit,
            existing_lines,
            replace,
        )


def _split_lines_for_trackable_and_preorder(
    variants: Iterable["ProductVariant"], quantities: Iterable[int]
) -> Tuple[
    Iterable["ProductVariant"], Iterable[int], Iterable["ProductVariant"], Iterable[int]
]:
    """Return variants and quantities splitted by "is_preorder_active"."""
    stock_variants, stock_quantities = [], []
    preorder_variants, preorder_quantities = [], []

    for variant, quantity in zip(variants, quantities):
        if variant.is_preorder_active():
            preorder_variants.append(variant)
            preorder_quantities.append(quantity)
        else:
            stock_variants.append(variant)
            stock_quantities.append(quantity)
    return (
        stock_variants,
        stock_quantities,
        preorder_variants,
        preorder_quantities,
    )


def _check_quantity_limits(
    variant: "ProductVariant", quantity: int, global_quantity_limit: Optional[int]
) -> Optional[NoReturn]:
    quantity_limit = variant.quantity_limit_per_customer or global_quantity_limit

    if quantity_limit is not None and quantity > quantity_limit:
        raise ValidationError(
            {
                "quantity": ValidationError(
                    (
                        f"Cannot add more than {quantity_limit} "
                        f"times this item: {variant}."
                    ),
                    code=CheckoutErrorCode.QUANTITY_GREATER_THAN_LIMIT.value,
                )
            }
        )
    return None


def check_stock_quantity_bulk(
    variants: Iterable["ProductVariant"],
    country_code: str,
    quantities: Iterable[int],
    channel_slug: str,
    global_quantity_limit: Optional[int],
    additional_filter_lookup: Optional[Dict[str, Any]] = None,
    existing_lines: Iterable["CheckoutLineInfo"] = None,
    replace=False,
    check_reservations: bool = False,
):
    """Validate if there is stock available for given variants in given country.

    :raises InsufficientStock: when there is not enough items in stock for a variant.
    """
    filter_lookup = {"product_variant__in": variants}
    if additional_filter_lookup is not None:
        filter_lookup.update(additional_filter_lookup)

    all_variants_stocks = (
        Stock.objects.for_country_and_channel(country_code, channel_slug)
        .filter(**filter_lookup)
        .annotate_available_quantity()
    )

    variant_stocks: Dict[int, List[Stock]] = defaultdict(list)
    for stock in all_variants_stocks:
        variant_stocks[stock.product_variant_id].append(stock)

    if check_reservations:
        variant_reservations = get_reserved_quantity_bulk(
            all_variants_stocks,
            [line.line for line in existing_lines] if existing_lines else [],
        )
    else:
        variant_reservations = defaultdict(int)

    insufficient_stocks: List[InsufficientStockData] = []
    variants_quantities = {
        line.variant.pk: line.line.quantity for line in existing_lines or []
    }
    for variant, quantity in zip(variants, quantities):

        if not replace:
            quantity += variants_quantities.get(variant.pk, 0)

        stocks = variant_stocks.get(variant.pk, [])
        available_quantity = sum(
            [stock.available_quantity for stock in stocks]  # type: ignore
        )
        available_quantity = max(
            available_quantity - variant_reservations[variant.pk], 0
        )

        if quantity > 0:
            _check_quantity_limits(variant, quantity, global_quantity_limit)

            if not stocks:
                insufficient_stocks.append(
                    InsufficientStockData(
                        variant=variant, available_quantity=available_quantity
                    )
                )
            elif variant.track_inventory and quantity > available_quantity:
                insufficient_stocks.append(
                    InsufficientStockData(
                        variant=variant,
                        available_quantity=available_quantity,
                    )
                )

    if insufficient_stocks:
        raise InsufficientStock(insufficient_stocks)


def get_channel_availbility_and_allocations(
    variants: Iterable["ProductVariant"], quantities: Iterable[int], channel_slug
):
    all_variants_channel_listings = (
        ProductVariantChannelListing.objects.filter(variant__in=variants)
        .annotate_preorder_quantity_allocated()
        .annotate(
            available_preorder_quantity=F("preorder_quantity_threshold")
            - Coalesce(Sum("preorder_allocations__quantity"), 0),
        )
        .select_related("channel")
    )
    variants_channel_availability = {
        channel_listing.variant_id: (
            channel_listing.available_preorder_quantity,
            channel_listing.preorder_quantity_threshold,
        )
        for channel_listing in all_variants_channel_listings
        if channel_listing.channel.slug == channel_slug
    }

    variant_channels: Dict[int, List[ProductVariantChannelListing]] = defaultdict(list)
    for channel_listing in all_variants_channel_listings:
        variant_channels[channel_listing.variant_id].append(channel_listing)

    variants_global_allocations = {
        variant_id: sum(
            channel_listing.preorder_quantity_allocated  # type: ignore
            for channel_listing in channel_listings
        )
        for variant_id, channel_listings in variant_channels.items()
    }
    return variants_channel_availability, variants_global_allocations


def check_preorder_threshold(variant, quantity, channel_slug):
    (
        variants_channel_availability,
        variants_global_allocations,
    ) = get_channel_availbility_and_allocations([variant], [quantity], channel_slug)

    insufficient_stocks: List[InsufficientStockData] = []

    if variants_channel_availability[variant.id][1] is not None:
        if quantity > variants_channel_availability[variant.id][0]:
            insufficient_stocks.append(
                InsufficientStockData(
                    variant=variant,
                    available_quantity=variants_channel_availability[variant.id][0],
                )
            )

        if variant.preorder_global_threshold is not None:
            global_availability = (
                variant.preorder_global_threshold
                - variants_global_allocations[variant.id]
            )
            if quantity > global_availability:
                insufficient_stocks.append(
                    InsufficientStockData(
                        variant=variant,
                        available_quantity=global_availability,
                    )
                )

    if insufficient_stocks:
        raise InsufficientStock(insufficient_stocks)


def check_preorder_threshold_bulk(
    variants: Iterable["ProductVariant"],
    quantities: Iterable[int],
    channel_slug: str,
    global_quantity_limit: Optional[int],
    existing_lines: Iterable["CheckoutLineInfo"] = None,
    replace: bool = False,
):
    """Validate if there is enough preordered variants according to thresholds.

    :raises InsufficientStock: when there is not enough available items for a variant.
    """
    (
        variants_channel_availability,
        variants_global_allocations,
    ) = get_channel_availbility_and_allocations(variants, quantities, channel_slug)

    insufficient_stocks: List[InsufficientStockData] = []
    variants_quantities = {
        line.variant.pk: line.line.quantity for line in existing_lines or []
    }
    for variant, quantity in zip(variants, quantities):

        if not replace:
            quantity += variants_quantities.get(variant.pk, 0)

        if quantity > 0:
            _check_quantity_limits(variant, quantity, global_quantity_limit)

        if variants_channel_availability[variant.id][1] is not None:
            if quantity > variants_channel_availability[variant.id][0]:
                insufficient_stocks.append(
                    InsufficientStockData(
                        variant=variant,
                        available_quantity=variants_channel_availability[variant.id][0],
                    )
                )

        if variant.preorder_global_threshold is not None:
            global_availability = (
                variant.preorder_global_threshold
                - variants_global_allocations[variant.id]
            )
            if quantity > global_availability:
                insufficient_stocks.append(
                    InsufficientStockData(
                        variant=variant,
                        available_quantity=global_availability,
                    )
                )

    if insufficient_stocks:
        raise InsufficientStock(insufficient_stocks)


def get_available_quantity(
    variant: "ProductVariant",
    country_code: str,
    channel_slug: str,
    checkout_lines: Optional[List["CheckoutLine"]] = None,
    check_reservations: bool = False,
) -> int:
    """Return available quantity for given product in given country."""
    stocks = Stock.objects.get_variant_stocks_for_country(
        country_code, channel_slug, variant
    )
    if not stocks:
        return 0
    return _get_available_quantity(stocks, checkout_lines, check_reservations)


def is_product_in_stock(
    product: "Product", country_code: str, channel_slug: str
) -> bool:
    """Check if there is any variant of given product available in given country."""
    stocks = Stock.objects.get_product_stocks_for_country_and_channel(
        country_code, channel_slug, product
    ).annotate_available_quantity()
    return any(stocks.values_list("available_quantity", flat=True))


def get_reserved_quantity(
    stocks: StockQuerySet, lines: Optional[List["CheckoutLine"]] = None
) -> int:
    result = (
        Reservation.objects.filter(
            stock__in=stocks,
        )
        .not_expired()
        .exclude_checkout_lines(lines)
        .aggregate(
            quantity_reserved=Coalesce(Sum("quantity_reserved"), 0),
        )
    )  # type: ignore

    return result["quantity_reserved"]


def get_reserved_quantity_bulk(
    stocks: Iterable[Stock],
    checkout_lines: Iterable["CheckoutLine"],
) -> Dict[int, int]:
    reservations: Dict[int, int] = defaultdict(int)
    if not stocks:
        return reservations

    result = (
        Reservation.objects.filter(
            stock__in=stocks,
        )
        .not_expired()
        .exclude_checkout_lines(checkout_lines)
        .values("stock_id")
        .annotate(
            quantity_reserved=Coalesce(Sum("quantity_reserved"), 0),
        )
    )  # type: ignore

    stocks_variants = {stock.id: stock.product_variant_id for stock in stocks}
    for stock_reservations in result:
        variant_id = stocks_variants.get(stock_reservations["stock_id"])
        if variant_id:
            reservations[variant_id] += stock_reservations["quantity_reserved"]

    return reservations
