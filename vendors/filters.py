import django_filters

from .models import Product


class ProductFilter(django_filters.FilterSet):
    # Custom filter for dietary_tags (JSON list)
    dietary_tags = django_filters.CharFilter(method='filter_dietary_tags')

    # Rating filters (exact and range)
    rating = django_filters.NumberFilter()
    rating__gte = django_filters.NumberFilter(field_name='rating', lookup_expr='gte')
    rating__lte = django_filters.NumberFilter(field_name='rating', lookup_expr='lte')

    # Discount filters (with alias for frontend)
    is_discounted = django_filters.BooleanFilter()
    discount = django_filters.BooleanFilter(field_name='is_discounted')  # alias so ?discount=true works

    class Meta:
        model = Product
        fields = ['category', 'vendor']  # simple FK filters still work

    def filter_dietary_tags(self, queryset, name, value):  # noqa: ARG002 - signature kept for caller/framework contract
        """
        Filter products whose dietary_tags contain the given value (case‑insensitive).
        Works on both SQLite and PostgreSQL JSONField.
        """
        return queryset.filter(dietary_tags__icontains=value)
