# Internal service ports (used for Docker network communication)
INTERNAL_VIEW_PORT = 7443
INTERNAL_API_PORT = 8443
INTERNAL_AUTH_PORT = 9090
INTERNAL_REDIS_PORT = 6379
INTERNAL_DB_PORT = 5432
INTERNAL_CADDY_ADMIN_PORT = 2019

# Docker service name prefixes
SERVICE_PREFIX_PRODUCTION = "nixopus"
SERVICE_PREFIX_STAGING = "nixopus-staging"

# Service name suffixes (combined with prefix to form full service names)
SERVICE_VIEW = "view"
SERVICE_API = "api"
SERVICE_AUTH = "auth"
SERVICE_DB = "db"
SERVICE_REDIS = "redis"
SERVICE_CADDY = "caddy"


def get_service_name(service: str, staging: bool = False) -> str:
    prefix = SERVICE_PREFIX_STAGING if staging else SERVICE_PREFIX_PRODUCTION
    return f"{prefix}-{service}"


def get_internal_service_url(service: str, port: int, staging: bool = False) -> str:
    service_name = get_service_name(service, staging)
    return f"{service_name}:{port}"

def get_view_service_url(staging: bool = False) -> str:
    return get_internal_service_url(SERVICE_VIEW, INTERNAL_VIEW_PORT, staging)


def get_api_service_url(staging: bool = False) -> str:
    return get_internal_service_url(SERVICE_API, INTERNAL_API_PORT, staging)


def get_auth_service_url(staging: bool = False) -> str:
    return get_internal_service_url(SERVICE_AUTH, INTERNAL_AUTH_PORT, staging)


def get_db_service_name(staging: bool = False) -> str:
    return get_service_name(SERVICE_DB, staging)


def get_redis_service_url(staging: bool = False, password: str = "changeme") -> str:
    service_name = get_service_name(SERVICE_REDIS, staging)
    return f"redis://default:{password}@{service_name}:{INTERNAL_REDIS_PORT}"


def get_caddy_endpoint(staging: bool = False) -> str:
    service_name = get_service_name(SERVICE_CADDY, staging)
    return f"http://{service_name}:{INTERNAL_CADDY_ADMIN_PORT}"
