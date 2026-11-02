from django.core.management.base import BaseCommand

from vendors.tasks import refresh_category_bestsellers


class Command(BaseCommand):
    help = (
        'Compute category bestsellers (backfill): flag top sellers per marketplace '
        'Category row and rewrite CategoryBestseller snapshots. Run once after '
        'migration, then nightly via Celery beat.'
    )

    def add_arguments(self, parser):
        parser.add_argument('--days', type=int, default=30, help='Rolling window in days (default 30)')
        parser.add_argument('--top-n', type=int, default=20, help='Max flagged products per category')
        parser.add_argument(
            '--min-orders',
            type=int,
            default=3,
            help='Min distinct delivered orders for a product to qualify',
        )

    def handle(self, *args, **options):  # noqa: ARG002 - framework signature must stay fixed
        result = refresh_category_bestsellers(
            days=options['days'],
            top_n=options['top_n'],
            min_orders=options['min_orders'],
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Bestsellers computed: {result['categories']} categories, "
                f"{result['flagged']} products flagged (window={options['days']}d)"
            )
        )
