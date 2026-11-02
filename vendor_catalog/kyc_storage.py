from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.utils.deconstruct import deconstructible

if settings.USE_S3_MEDIA:
    from storages.backends.s3boto3 import S3Boto3Storage

    @deconstructible
    class KycDocumentStorage(S3Boto3Storage):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('bucket_name', settings.AWS_STORAGE_BUCKET_NAME)
            kwargs.setdefault('location', 'vendor-kyc')
            kwargs.setdefault('default_acl', 'private')
            kwargs.setdefault('querystring_auth', True)
            kwargs.setdefault('querystring_expire', 900)
            kwargs.setdefault('file_overwrite', False)
            encryption = {'ServerSideEncryption': 'aws:kms'} if settings.AWS_S3_KMS_KEY_ARN else {
                'ServerSideEncryption': 'AES256'
            }
            if settings.AWS_S3_KMS_KEY_ARN:
                encryption['SSEKMSKeyId'] = settings.AWS_S3_KMS_KEY_ARN
            kwargs.setdefault('object_parameters', encryption)
            super().__init__(*args, **kwargs)
else:

    @deconstructible
    class KycDocumentStorage(FileSystemStorage):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault('location', settings.KYC_MEDIA_ROOT)
            kwargs.setdefault('base_url', '')
            super().__init__(*args, **kwargs)


kyc_document_storage = KycDocumentStorage()
