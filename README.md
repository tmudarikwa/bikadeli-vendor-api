# Vendor API

Standalone Django role API container. Vendor profiles, marketplace categories/products, and restaurant menu categories/items/options/images are extracted into `vendor_catalog`, which uses this service's own database and stores identity/vendor/category IDs as scalar references (not cross-database foreign keys). The marketplace category hierarchy and vendor-specific overrides are retained, including duplicate category names. Other vendor workflows remain on their existing backend owner until individually extracted.

Shared `/api/v1/auth/register/`, `/api/v1/auth/login/`, `/api/v1/auth/logout/`, `/api/v1/auth/logout-all/`, and token-refresh requests are routed to `identity-service`. The legacy `/api/v1/vendors/login/` path is routed to an identity-service endpoint that only issues vendor tokens, preserving the inactive-account response. Vendor-specific `/api/v1/vendors/register/` is served by vendor-api: it creates an inactive identity through a token-protected identity-service endpoint, then idempotently creates the vendor-owned pending profile. A retry can complete profile creation if the identity request succeeded but the vendor database was unavailable. The shared `VENDOR_REGISTRATION_TOKEN` must be configured in both services.

Vendor food-order list/detail and accept/reject/preparing/ready actions can be routed to orders-service with `VENDOR_ORDER_SERVICE_ENABLED=true` and `ORDERS_SERVICE_URL`. The gateway keeps the monolith compatibility route when the flag is off and returns an explicit upstream error rather than falling back when enabled. Orders-service scopes every route to the authenticated vendor identity. Paid-order rejection uses finance-service's signed, idempotent refund workflow and fails closed unless finance confirms completion. Vendor preparation-time updates are owned here: orders-service calls the signed `POST /internal/v1/vendors/preparation-time/` contract, which updates `VendorProfile.preparation_time_mins` and records an order-keyed audit row. Identical retries are idempotent; conflicting retries fail with `409`. Configure the same private `VENDOR_PROFILE_UPDATE_SECRET` (at least 32 characters) in orders-service and vendor-api. If the callback is unavailable, the order acceptance fails without changing its status; a successfully applied profile update is retained and safely recognized on a retry.

Admin verification is available through the admin-api compatibility route `POST /api/v1/admin/vendors/<vendor_id>/verify/`. It continues to proxy to the monolith by default. To opt in to the extracted flow, configure both `VENDOR_APPROVAL_SERVICE_ENABLED=true` and `ADMIN_IDENTITY_AUTH_ENABLED=true`, plus `VENDOR_SERVICE_URL`, `IDENTITY_SERVICE_URL`, `ADMIN_AUTHORIZATION_TOKEN`, and a private `VENDOR_ADMIN_TOKEN` of at least 32 characters in admin-api; configure the same `VENDOR_ADMIN_TOKEN`, `IDENTITY_SERVICE_URL`, and shared `VENDOR_REGISTRATION_TOKEN` in vendor-api/identity-service. Vendor-api also needs `CHANNEL_REDIS_URL` (or `REDIS_URL`) configured to the same Redis channel layer used by the WebSocket consumers. Until both flags are enabled, admin-api keeps the legacy authorization and monolith route intact. The gateway asks identity-service to authorize `vendors:verify` and activate only a vendor identity, then vendor-api updates its profile and writes an audit row. Verifying a vendor also activates the vendor profile for marketplace visibility; unverifying does not deactivate the account. A verification-state transition emits the legacy best-effort `Vendor Status` WebSocket event to `user_<identity-id>` after the profile transaction commits. Cross-service activation and local profile update are not one transaction: the operations are idempotent, so retry a failed verification if identity activation succeeded but the profile update did not. No profile migration or production cutover has been performed.

## Vendor KYC documents and reviews

Vendor-api now owns the opt-in vendor document, verification, tax, and sanctions records. Owner-scoped routes include `GET|POST /api/v1/vendors/kyc/documents/`, `GET|DELETE /api/v1/vendors/kyc/documents/<id>/`, `GET /api/v1/vendors/kyc/verification/`, `GET|POST /api/v1/vendors/kyc/tax/`, `GET|PATCH|PUT|DELETE /api/v1/vendors/kyc/tax/<id>/`, `GET /api/v1/vendors/kyc/sanctions/`, `GET /api/v1/vendors/kyc/sanctions/<id>/`, `GET /api/v1/vendors/kyc/dashboard/dashboard/`, and `POST /api/v1/vendors/kyc/dashboard/refresh-kyc/`. Tax identifiers are encrypted at rest and only returned in masked form; sanctions routes are read-only to vendors. Admin sanctions decisions are accepted only on token-protected internal routes, require an actor and review reason, and are audit-logged. Confirmed sanctions matches are excluded from public vendor, product, and menu results. KYC and tax document uploads are limited to 10 MiB, validated against extension and file content, and scanned by ClamAV when enabled. Documents use private filesystem storage outside `MEDIA_ROOT` in development (without a public URL) or private S3-compatible storage in production; S3 access links expire after 15 minutes and objects use SSE-KMS.

The extracted owner routes proxy to the monolith unless `VENDOR_KYC_SERVICE_ENABLED=true` is set in vendor-api. Production requires `USE_S3_MEDIA=true`, `AWS_STORAGE_BUCKET_NAME`, `AWS_S3_KMS_KEY_ARN`, and a reachable ClamAV scanner (`CLAMAV_ENABLED=true`). For local filesystem use, mount persistent storage at `KYC_MEDIA_ROOT` (default `/app/private-kyc`), outside public `MEDIA_ROOT`; startup rejects a local KYC root nested under `MEDIA_ROOT`. Admin queue/detail and approve/reject routes keep proxying to the monolith unless `VENDOR_KYC_SERVICE_ENABLED=true` and `ADMIN_IDENTITY_AUTH_ENABLED=true` are both set in admin-api. The admin gateway authorizes queue/detail reads with `vendors:verify` and review mutations with `vendors:write`; it forwards actions to token-protected vendor-api internal routes. Configure the same private `VENDOR_ADMIN_TOKEN` (at least 32 characters) and `VENDOR_SERVICE_URL` in the relevant services.

The compliance summary, tax, and sanctions reads now use vendor-api-owned records when `VENDOR_KYC_SERVICE_ENABLED=true`; when disabled, these routes continue proxying to the monolith. Tax identifiers are never returned in plaintext; reviewer names are not available, though reviewer IDs and audit actor emails are retained. Sanctions decisions queue a catalog snapshot refresh, and confirmed matches are sent to orders-service as inactive vendors. The legacy sanctions task safely creates pending records but is not wired to a real screening provider or vendor-api. Historical sanctions/tax/KYC records and document files are not migrated.

Finance's internal `GET /internal/v1/admin/vendors/<owner_user_id>/payout-safety/` endpoint returns the legacy-compatible compliance hold codes for a vendor and checks Identity account activity before reporting eligibility. It is protected by `VENDOR_ADMIN_TOKEN` and the independent `VENDOR_PAYOUT_SAFETY_ENABLED` gate. Enablement also requires the Identity service and the shared `IDENTITY_USER_LOOKUP_TOKEN`; Finance independently fails closed if this endpoint is disabled, unavailable, or returns malformed data. Keep both payout gates off until compliance records and private KYC evidence have been reconciled.

`check_vendor_kyc_expiry` reports changes by default; pass `--apply` to mark required expired documents `EXPIRED`, refresh KYC verification, close currently open affected vendors, record audit logs, enqueue idempotent 30/7/1-day alerts, and publish pending notifications through the shared event contract. Vendor-owned notifications, including approval status, are listed and marked read through `GET|PATCH /api/v1/vendors/notifications/`; the vendor web page combines them with the legacy order-notification feed. Delivery failures remain in the vendor outbox for retry. The command does not automatically reopen previously closed vendors. A daily, non-overlapping platform CronJob for `python manage.py check_vendor_kyc_expiry --apply` is not deployed; this repository has no platform manifests.

Required document types and historical compliance data must be reconciled/backfilled before cutover; the service does not infer historical document requirements. Keep production flags disabled until real screening and reviewer identity parity, the scheduled worker, required-document rules, historical data, and remaining workflows are reconciled. No production migration, preference backfill, or traffic cutover has been run.

Historical KYC documents, verification records, tax info, sanctions screenings, and expiry logs can be copied with `python vendor_api/manage.py backfill_vendor_compliance`. This defaults to a read-only dry run; `--apply` is required to copy rows/files. It requires an explicit, distinct `SOURCE_DATABASE_URL` and `DATABASE_URL`, vendor profiles already backfilled with matching IDs, `SOURCE_KYC_MEDIA_ROOT` for legacy private files, and `SOURCE_FIELDS_ENCRYPTION_KEY` plus `SOURCE_FIELDS_ENCRYPTION_SALT` when source tax identifiers are encrypted. It re-encrypts tax identifiers with `VENDOR_FIELDS_ENCRYPTION_KEY`, checks IDs/relationships and private-file checksums, reports keyed row checksums, and refuses conflicting rows/files or missing/unverifiable source media before inserting records. Applying it is an operational data change and must be rehearsed against non-production copies; it has not been run against production.

The old `vendors/` app folder is a migration-source snapshot only. Its model imports depend on the monolith; the runtime and Docker image do not load it. Runtime marketplace reads use only `vendor_catalog` models in the vendor database; they do not proxy through the monolith or use cross-service ORM references. Vendor profile/category/product rows have not been backfilled or cut over; do not point production vendor traffic at this database until rows are migrated and parity is verified.

## Public marketplace reads

The service implements public, read-only marketplace endpoints:

- `GET /api/v1/marketplace/products/` and `GET /api/v1/marketplace/products/<id>/`
- `GET /api/v1/marketplace/categories/` and `GET /api/v1/marketplace/categories/<id>/`

Product listings return a page-number response with 20 items per page and expose fields held by the vendor-owned profile/catalog schema. Supported filters include `product_type`, `category`, `vendor`, `is_discounted`/`discount`, `min_price`, `max_price`, `pickup`, `open_now`, `free_delivery`, `rating`, `max_time`, `popular`, `in_stock`, and `new`; `search` and `q` search name, description, brand, and barcode. Ordering supports `price`, `created_at`, `name`, `rating`, and `is_popular`. By default, unavailable products, inactive/unverified vendors, tracked products with no stock, and restaurant/fast-food products are excluded from browse results. Search and an explicit `vendor` filter include restaurant/fast-food products. Category listing returns active shared root categories and their active children.

`GET /api/v1/vendors/public/products/lookup/?ids=<comma-separated-ids>` returns bounded batches of up to 200 products with only ID, name, price, and first-image URL for customer product-favourite reads. Product IDs not found in vendor-api are omitted; the customer API treats incomplete lookup results as an upstream error.

This is intentionally not a claim of full marketplace parity. The current service schema has no vendor rating, bestseller-rank data, product unit, or product variants; those response fields and bestseller ordering are not provided. The `rating` filter therefore uses the owned product rating. Product images are vendor-owned and included in product responses, but product variants, sanctions/tax compliance, order and fulfillment operations, analytics/bestseller computation, payouts, and related history remain unextracted and owned by their existing systems. KYC documents are implemented behind the opt-in described above but are not yet backfilled or cut over. Profile logos/covers and other uploaded media are also not copied by the catalog backfill.

Vendor product create/update accepts repeated multipart `images` files on `POST|PUT|PATCH /api/v1/vendors/products/`; supplying `images` on update replaces that product's image set, while omitting it leaves images unchanged. Product responses contain ordered image records with public file URLs at `/api/v1/vendors/product-images/<image_id>/file/`. Product image uploads use the same decoded-image, MIME/extension, and 5 MiB validation as restaurant menu images. These URLs must be reachable by clients: configure `VENDOR_PUBLIC_BASE_URL` to the public gateway origin that routes the product-image path to vendor-api. The customer API gateway proxies product-image file requests. Persist vendor-api `MEDIA_ROOT` storage across deployments.

## Restaurant menu API

The service owns restaurant menu categories, items, options, and additional item images in `RestaurantMenuCategory`, `RestaurantMenuItem`, `RestaurantMenuItemOption`, and `RestaurantMenuItemImage`. IDs for those rows are preserved during backfill. `vendor_id` and `category_id` remain scalar IDs; the service does not add cross-domain foreign keys. Writes resolve the authenticated identity's `VendorProfile` and force its ID as `vendor_id`.

Vendor-owner CRUD requires a valid access token and an existing vendor profile for that token's user ID:

- `GET|PUT /api/v1/vendors/profile/me/` exposes and updates the vendor's own profile. Vendors cannot change admin-managed `delivery_fee` or `minimum_order`; those fields are rejected with HTTP 400.
- `POST /api/v1/vendors/profile/toggle-open/` accepts `{"is_open": true|false}` and updates only the authenticated vendor's availability.
- `GET|POST /api/v1/vendors/menu-items/`
- `GET|PUT|PATCH|DELETE /api/v1/vendors/menu-items/<id>/`

The create/update body supports `category_id`, `name`, `description`, `price`, `image` (legacy path string), `is_available`, `is_popular`, `preparation_time_mins`, and nested `options` (`name`, `price_adjustment`). Supplying `options` on update replaces the full option set; omitting it leaves options unchanged. DELETE is a soft delete: it marks the item unavailable, retains its ID, fields, and options, and keeps it visible in the owner's menu listing. The web-vendor legacy contract is also implemented locally, before the compatibility catch-all:

- `GET /api/v1/vendors/menu/` returns `{results, count}` categories. Each category includes `id`, `name`, `description`, `order`, and `items`; each item includes `id`, `name`, `description`, `price`, `is_available`, `image`, `images`, `options`, `category_name`, `vendor`, `vendor_id`, and `vendor_name`.
- `GET /api/v1/vendors/public/vendors/<vendor_id>/menu/` returns the active vendor's active categories and available items as a category array, matching the backend public menu action.
- `POST /api/v1/vendors/menu/add-category/` creates an owner-scoped category from `name` and optional `description` (optional `order` is also accepted).
- `POST /api/v1/vendors/menu/add-item/` creates an item in an active category owned by the authenticated vendor and returns `{message, item}`.
- `GET /api/v1/vendors/menu/<id>/` returns the owner's item detail, including `description`, `category_name`, `vendor_id`, and image objects `{id, image, alt_text, sort_order, vendor_id}`.
- `PUT /api/v1/vendors/menu/<id>/update-item/` updates item fields and returns `{message, item}`.
- `DELETE /api/v1/vendors/menu/<id>/delete-item/` is a soft delete: it marks the item unavailable, retains its ID/data/images, and keeps it visible in owner listings. The newer `/menu-items/<id>/` DELETE does the same. Sync then sends `is_available=false` to orders-service.
- `POST /api/v1/vendors/menu/<id>/upload-images/` accepts repeated multipart `images` and returns `{message, created, errors}`; invalid individual files appear in `errors` while valid files in the same request are saved.
- `DELETE /api/v1/vendors/menu/<id>/delete-image/` accepts JSON `{image_id}` and only deletes an image owned by that item/vendor. The older single-image `POST .../<id>/upload-image/` is retained too.

Image uploads are validated server-side: decoded JPEG, PNG, WEBP, or GIF; matching MIME type and file extension; at most 5 MiB per file. Image paths are stored in the vendor service's `MEDIA_ROOT` (default `/app/media`) and served through vendor-api item/image file endpoints; mount persistent storage at that location for production. Image-file URLs in item responses point to those endpoints. Image storage is service-owned, not shared with the monolith.

The old backend's `GET|PUT|PATCH|DELETE /api/v1/vendors/menu/<id>/` is ambiguous with the web consumer's item-detail GET at the same path. For compatibility, GET prefers a matching owner item and otherwise returns a category with that ID; PUT/PATCH/DELETE target the owner's category. The explicit `/menu/categories/<id>/` route is available for category detail/update/deactivation without this ambiguity. Unrecognized `/api/v1/vendors/menu/...` paths return a vendor-api 404 instead of falling through to the monolith. Categories are deactivated rather than hard-deleted so item category IDs remain stable.

Public, read-only menu routes are available without authentication:

- `GET /api/v1/vendors/public/menu-items/` (optional `vendor_id` and `available=true` filters)
- `GET /api/v1/vendors/public/menu-items/<id>/`

Only menus belonging to active, verified vendor profiles are exposed. Responses include item/option IDs, price and availability, scalar category/vendor IDs, vendor name/open/active/minimum-order snapshot values, and preparation time. Public responses omit private vendor address and owner user ID. The sync command separately supplies both fields required by the trusted orders-service snapshot contract.

From the workspace root:

```powershell
docker build -f vendor_api/Dockerfile -t bikadeli-vendor-api:local .
```

Set `DJANGO_SECRET_KEY`, `DATABASE_URL`, `JWT_PUBLIC_KEYS_JSON`, `JWT_ISSUER`, `IDENTITY_SERVICE_URL`, `VENDOR_FIELDS_ENCRYPTION_KEY` (a Fernet key), and `ALLOWED_HOSTS` to the service's allowed hostnames, then run the image on port `8000`. Production requires `IDENTITY_SERVICE_URL`, an issuer, and RSA public verification keys; keep `JWT_VALIDATE_IDENTITY_SESSIONS` enabled so logout and account deactivation take effect on vendor requests. Never provide the identity private signing key to vendor-api. `/health/` is liveness; `/health/ready/` checks the vendor-owned database. Local Compose uses the shared development HS256 key only with `DJANGO_DEBUG=true`. Local Python setup: create a virtual environment, install `vendor_api/requirements.txt`, then run `python vendor_api/manage.py check`.

Apply the vendor-owned schema with `python vendor_api/manage.py migrate`. Production must set `DATABASE_URL` to the vendor database, `JWT_PUBLIC_KEYS_JSON` to a JSON object mapping accepted key IDs to PEM-encoded RSA public keys, and `VENDOR_PUBLIC_BASE_URL` to the externally reachable gateway origin for image URLs. The verifier accepts RS256 tokens only in production; keep overlapping public keys in the map during identity key rotation.

## Controlled catalog data copy

`backfill_vendor_catalog` reads the monolith tables `vendors_vendorprofile`, `vendors_category`, `vendors_product`, and `vendors_productimage` into this service's equivalent tables. It preserves profile/category/product/image IDs and relationships, including repeated category names, parent/vendor category references, and image ordering/alt text. Product `menu_item_id` is retained as `legacy_menu_item_id` (an external numeric reference, not a cross-service foreign key). Set `SOURCE_MEDIA_ROOT` to the monolith media root to copy referenced product-image files into vendor-api `MEDIA_ROOT`. The dry run reports missing product-image files, and `--apply` refuses to proceed if any are missing from both source and destination storage. The command does not copy KYC/payout data, profile logos/covers, menu items/options/images, or related history; those remain with their existing owners.

Set `SOURCE_DATABASE_URL` to a read-only monolith database connection and `DATABASE_URL` to a distinct vendor database. For encrypted legacy business phones, also set `SOURCE_FIELDS_ENCRYPTION_KEY` and `SOURCE_FIELDS_ENCRYPTION_SALT` to the legacy field-encryption credentials, and set a separate `VENDOR_FIELDS_ENCRYPTION_KEY` for destination encryption. Never reuse the monolith key for the new service. Run a dry run first; it is read-only and reports counts and SHA-256 checksums:

```powershell
python vendor_api/manage.py backfill_vendor_catalog
```

Only after reviewing the report and validating the destination target should an operator explicitly apply the insert-only copy:

```powershell
python vendor_api/manage.py backfill_vendor_catalog --apply
```

The operation is transactional, rejects conflicting destination IDs/owners and invalid references, and is idempotent when existing rows match. It never updates existing rows or changes routing. A successful copy is not itself a production cutover; reconcile the reported counts/checksums and validate the remaining domain dependencies before changing traffic.

### Restaurant menu items and options

`backfill_restaurant_menu` reads the monolith `vendors_menucategory`, `vendors_menuitem`, `vendors_menuitemoption`, and `vendors_menuitemimage` tables, preserving each category/item/option/image ID, scalar vendor/category IDs, fields, and option/image-to-item IDs. It does not require a cross-database vendor foreign key. Source items/options/images pointing to absent categories/items and destination ID conflicts are rejected. Existing matching rows are left untouched; the command is insert-only and transactional. It defaults to a dry run:

```powershell
python vendor_api/manage.py backfill_restaurant_menu
python vendor_api/manage.py backfill_restaurant_menu --apply
```

Use the same distinct `SOURCE_DATABASE_URL` and vendor `DATABASE_URL` safeguards as the catalog copy above. Set `SOURCE_MEDIA_ROOT` to the monolith's media root to copy referenced legacy item and additional image files under their existing storage keys into vendor-api `MEDIA_ROOT`. The command reports missing files; rows and IDs are preserved even if a source media file is unavailable, so reconcile `media_missing` before cutover. Do not run `--apply` against production without an independently reviewed dry-run report and destination backup.

### Orders-service snapshot sync

The service can synchronize current vendor-owned items and options to the existing orders-service endpoint `/api/v1/orders/internal/catalog/menu-items/`. Configure `FOOD_CATALOG_SYNC_URL` to that full endpoint URL and `FOOD_CATALOG_SYNC_TOKEN` to the same shared secret configured by orders-service. The command sends `X-Catalog-Sync-Token`, retries transient request/5xx/429 failures, reports failures, and can be rerun safely because the existing endpoint upserts by menu-item ID:

```powershell
python vendor_api/manage.py sync_food_menu_snapshots
python vendor_api/manage.py sync_food_menu_snapshots --vendor-id 71 --retries 5
```

Vendor profile and restaurant-menu item create/update operations create a durable, coalesced catalog-sync outbox event in the vendor database. Availability changes therefore reach orders-service along with menu changes. A deployment worker must run the retryable pending-event mode periodically:

```powershell
python vendor_api/manage.py sync_food_menu_snapshots --pending-only
```

The local Compose stack runs a dedicated `vendor-menu-sync` worker every 60 seconds. Production must deploy and monitor exactly one scheduled worker per environment to avoid overlapping snapshot pushes. The outbox records delivery completion and retains failed events for retry. This is eventual consistency, not a synchronous cross-service transaction; until delivery succeeds, orders-service may continue using the previous vendor availability snapshot. Soft-deleted items are retained and sent as `is_available=false`, allowing orders-service checkout to reject them while preserving the snapshot ID. Do not hard-delete menu rows outside the vendor API: the existing orders-service endpoint has no deletion/tombstone operation, so a hard-deleted item may remain available in its snapshot until that contract is extended. Orders-service remains responsible for its local snapshot and checkout reads; failed item IDs should be reconciled by rerunning the command.

## Vendor notifications and marketing preferences

Vendor-api now stores KYC-expiry and approval-status notifications in its own outbox/inbox; `GET|PATCH /api/v1/vendors/notifications/` is independent of the KYC feature flag. Verification-status changes create the notification in the same database transaction as the audit row. WebSocket publication remains best-effort; a failed publish leaves the inbox record pending. The vendor web page combines these vendor-owned records with the still-legacy order-notification feed.

Vendor notifications and marketing preferences are routed through the monolith gateway to vendor-api using `VENDOR_API_URL`; the gateway forwards the authenticated request and returns 503 when the service is not configured or unavailable. Configure `VENDOR_API_TIMEOUT_SECONDS` to tune the gateway timeout.

Vendor marketing opt-outs are stored at `GET|PUT /api/v1/vendors/notification-preferences/`. Before switching the monolith's sender to vendor-api, copy existing vendor opt-outs with the monolith command `python manage.py backfill_vendor_notification_preferences` (dry run) and then `--apply`. Configure the same private `VENDOR_NOTIFICATION_TOKEN` (at least 32 characters) in vendor-api and the monolith, plus `VENDOR_API_URL` in the monolith. The monolith flag `VENDOR_NOTIFICATION_PREFERENCES_SERVICE_ENABLED` defaults to false; only enable it after backfill and parity checks. When enabled, vendor marketing sends check vendor-api and fail closed (skip the send) if the preference service is unavailable. Non-vendor preferences continue to use the monolith-owned records. No production backfill or flag cutover has been run.

The daily platform CronJob for `python manage.py check_vendor_kyc_expiry --apply` is still not deployed. Order notifications and remaining vendor workflows also remain on the monolith; these notification changes do not complete vendor-service independence.
