import datetime
from collections.abc import Iterable
from typing import TYPE_CHECKING, Union
from uuid import UUID

from ...core.taxes import TaxedMoney, zero_taxed_money
from ...core.tracing import traced_atomic_transaction
from ...core.utils.events import call_event
from ...discount.utils.promotion import mark_active_catalogue_promotion_rules_as_dirty
from ...webhook.event_types import WebhookEventAsyncType
from ...webhook.utils import get_webhooks_for_event
from ..models import Product, ProductChannelListing

if TYPE_CHECKING:
    from django.db.models.query import QuerySet

    from ...order.models import Order, OrderLine
    from ..models import Category, ProductVariant


def calculate_revenue_for_variant(
    variant: "ProductVariant",
    start_date: Union[datetime.date, datetime.datetime],
    order_lines: Iterable["OrderLine"],
    orders_dict: dict[UUID, "Order"],
    currency_code: str,
) -> TaxedMoney:
    """Calculate total revenue generated by a product variant."""
    revenue = zero_taxed_money(currency_code)
    for order_line in order_lines:
        order = orders_dict[order_line.order_id]
        if order.created_at >= start_date:
            revenue += order_line.total_price
    return revenue


@traced_atomic_transaction()
def delete_categories(categories_ids: list[Union[str, int]], manager):
    """Delete categories and perform all necessary actions.

    Set products of deleted categories as unpublished, delete categories
    and update products minimal variant prices.
    """
    from ..models import Category, Product

    categories = Category.objects.select_for_update().filter(pk__in=categories_ids)
    categories.prefetch_related("products")

    products = Product.objects.none()
    for category in categories:
        products = products | collect_categories_tree_products(category)

    product_channel_listing = ProductChannelListing.objects.filter(product__in=products)
    product_channel_listing.update(is_published=False, published_at=None)
    products = list(products)

    category_instances = list(categories)
    categories.delete()
    webhooks = get_webhooks_for_event(WebhookEventAsyncType.CATEGORY_DELETED)
    for category in category_instances:
        call_event(manager.category_deleted, category, webhooks=webhooks)
    webhooks = get_webhooks_for_event(WebhookEventAsyncType.PRODUCT_UPDATED)
    for product in products:
        call_event(manager.product_updated, product, webhooks=webhooks)

    channel_ids = set(product_channel_listing.values_list("channel_id", flat=True))
    call_event(mark_active_catalogue_promotion_rules_as_dirty, channel_ids)


def collect_categories_tree_products(category: "Category") -> "QuerySet[Product]":
    """Collect products from all levels in category tree."""
    products = category.products.all()
    descendants = category.get_descendants()
    for descendant in descendants:
        products = products | descendant.products.all()
    return products


def get_products_ids_without_variants(products_list: list["Product"]) -> list[int]:
    """Return list of product's ids without variants."""
    products_ids = [product.id for product in products_list]
    products_ids_without_variants = Product.objects.filter(
        id__in=products_ids, variants__isnull=True
    ).values_list("id", flat=True)
    return list(products_ids_without_variants)