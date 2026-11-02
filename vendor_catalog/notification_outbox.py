import logging

from asgiref.sync import async_to_sync
from bikadeli_common.notifications import (
    build_user_notification_event,
    user_notification_group,
)
from channels.layers import get_channel_layer
from django.utils import timezone

from .models import VendorNotificationOutbox

logger = logging.getLogger(__name__)


def publish_pending_vendor_notifications(batch_size=500, notification_ids=None):
    channel_layer = get_channel_layer()
    pending_query = VendorNotificationOutbox.objects.filter(status='PENDING')
    if notification_ids is not None:
        pending_query = pending_query.filter(id__in=notification_ids)
    pending = pending_query.order_by('created_at', 'id')[:batch_size]
    published = 0
    failed = 0

    for notification in pending:
        try:
            if channel_layer is None:
                raise RuntimeError('No channel layer is configured for vendor notifications.')
            async_to_sync(channel_layer.group_send)(
                user_notification_group(notification.recipient_user_id),
                build_user_notification_event(
                    notification_id=notification.id,
                    title=notification.title,
                    body=notification.body,
                    data=notification.data,
                    priority=notification.priority,
                ),
            )
        except Exception as exc:
            notification.attempts += 1
            notification.last_error = str(exc)[:300]
            notification.save(update_fields=['attempts', 'last_error'])
            logger.exception('Failed to publish vendor notification outbox row %s', notification.pk)
            failed += 1
        else:
            notification.status = 'PUBLISHED'
            notification.published_at = timezone.now()
            notification.attempts += 1
            notification.last_error = ''
            notification.save(update_fields=['status', 'published_at', 'attempts', 'last_error'])
            published += 1

    return {'published': published, 'failed': failed}
