import os
import ipaddress
import json
import shutil
import socket
from typing import Dict, Optional
from urllib.parse import urlparse, parse_qs, unquote, quote

from app.utils.config import (
    CADDY_ADMIN_PORT,
    CADDY_CONFIG_VOLUME,
    DEFAULT_BRANCH,
    DEFAULT_COMPOSE_FILE,
    DEFAULT_PATH,
    DEFAULT_REPO,
    NIXOPUS_CONFIG_DIR,
    SSH_FILE_PATH,
    get_active_config,
    get_config_value,
)
from app.utils.directory_manager import create_directory
from app.utils.file_manager import set_permissions
from app.utils.host_information import get_public_ip
from app.utils.protocols import LoggerProtocol

from .config_schema import ENV_VAR_KEYS
from .service_constants import (
    get_api_service_url,
    get_auth_service_url,
    get_caddy_endpoint,
    get_db_service_name,
    get_redis_service_url,
    get_view_service_url,
)


def resolve_hostname_to_ipv4(hostname: str) -> str:
    try:
        ip = ipaddress.ip_address(hostname)
        if isinstance(ip, ipaddress.IPv4Address):
            return hostname
        elif isinstance(ip, ipaddress.IPv6Address):
            try:
                addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
                if addr_info:
                    return addr_info[0][4][0]
            except (socket.gaierror, OSError):
                pass
            return hostname
    except ValueError:
        pass
    
    try:
        addr_info = socket.getaddrinfo(hostname, None, socket.AF_INET, socket.SOCK_STREAM)
        if addr_info:
            ipv4_address = addr_info[0][4][0]
            return ipv4_address
    except (socket.gaierror, OSError):
        pass
    
    return hostname


def parse_db_url(db_url: str) -> Dict[str, str]:
    parsed = urlparse(db_url)
    
    username = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    hostname = parsed.hostname or ""
    port = parsed.port or 5432
    database = unquote(parsed.path.lstrip("/") or "")
    
    query_params = parse_qs(parsed.query)
    ssl_mode = query_params.get("sslmode", ["disable"])[0] if query_params.get("sslmode") else "disable"
    
    resolved_host = resolve_hostname_to_ipv4(hostname)
    
    return {
        "HOST_NAME": resolved_host,
        "DB_PORT": str(port),
        "USERNAME": username,
        "PASSWORD": password,
        "DB_NAME": database,
        "SSL_MODE": ssl_mode,
        "POSTGRESQL_CONNECTION_URI": db_url,
    }


def construct_database_url(env_values: dict, staging: bool = False) -> str:
    host = env_values.get("HOST_NAME")
    if not host or (staging and not host.startswith(("http://", "https://"))):
        host = get_db_service_name(staging)
    port = env_values.get("DB_PORT", "5432")
    username = env_values.get("USERNAME", "postgres")
    password = env_values.get("PASSWORD", "changeme")
    db_name = env_values.get("DB_NAME", "postgres")
    ssl_mode = env_values.get("SSL_MODE", "disable")
    
    encoded_username = quote(username, safe="")
    encoded_password = quote(password, safe="")
    encoded_db_name = quote(db_name, safe="")
    
    db_url = f"postgresql://{encoded_username}:{encoded_password}@{host}:{port}/{encoded_db_name}"
    
    if ssl_mode and ssl_mode != "disable":
        db_url += f"?sslmode={ssl_mode}"
    
    return db_url


def is_custom_repo_or_branch(repo: Optional[str], branch: Optional[str]) -> bool:
    temp_config = get_active_config()
    default_repo = get_config_value(temp_config, DEFAULT_REPO)
    default_branch = get_config_value(temp_config, DEFAULT_BRANCH)

    repo_differs = repo is not None and repo != default_repo
    branch_differs = branch is not None and branch != default_branch

    return repo_differs or branch_differs


def get_host_ip_or_default(host_ip: Optional[str]) -> str:
    if host_ip:
        return host_ip
    return get_public_ip()


def get_full_source_path(config: dict) -> str:
    return os.path.join(get_config_value(config, NIXOPUS_CONFIG_DIR), get_config_value(config, DEFAULT_PATH))


def get_ssh_key_path(config: dict) -> str:
    return os.path.join(get_config_value(config, NIXOPUS_CONFIG_DIR), get_config_value(config, SSH_FILE_PATH))


def get_compose_file_path(config: dict, use_staging: bool) -> str:
    compose_path = os.path.join(get_config_value(config, NIXOPUS_CONFIG_DIR), get_config_value(config, DEFAULT_COMPOSE_FILE))
    if use_staging:
        return compose_path.replace("docker-compose.yml", "docker-compose-staging.yml")
    return compose_path


def get_proxy_port(config: dict, caddy_admin_port: Optional[int]) -> int:
    if caddy_admin_port is not None:
        return caddy_admin_port
    try:
        return int(get_config_value(config, CADDY_ADMIN_PORT))
    except (KeyError, ValueError):
        return 2019


def build_env_variable_map(
    host_ip: str,
    api_domain: Optional[str],
    view_domain: Optional[str],
    ssh_key_path: str,
    caddy_http_port: Optional[int] = None,
    caddy_https_port: Optional[int] = None,
    api_port: Optional[int] = None,
    view_port: Optional[int] = None,
    auth_port: Optional[int] = None,
    better_auth_url: Optional[str] = None,
    better_auth_secret: Optional[str] = None,
) -> Dict[str, str]:
    secure = api_domain is not None and view_domain is not None
    protocol = "https" if secure else "http"
    ws_protocol = "wss" if secure else "ws"
    
    if secure:
        api_host = api_domain
        view_host = view_domain
        # For domains, auth host defaults to API domain or can be overridden by better_auth_url
        auth_host = api_domain
    else:
        # When using IP addresses, use custom ports if provided, else fallback to caddy_http_port
        api_port_final = api_port if api_port is not None else (caddy_http_port if caddy_http_port is not None else 8443)
        view_port_final = view_port if view_port is not None else (caddy_http_port if caddy_http_port is not None else 7443)
        auth_port_final = auth_port if auth_port is not None else (caddy_http_port if caddy_http_port is not None else 9090)
        
        api_host = f"{host_ip}:{api_port_final}"
        view_host = f"{host_ip}:{view_port_final}"
        auth_host = f"{host_ip}:{auth_port_final}"
    
    view_domain_url = f"{protocol}://{view_host}"
    api_domain_url = f"{protocol}://{api_host}"
    auth_domain_url = f"{protocol}://{auth_host}"
    
    env_map = {
        "ALLOWED_ORIGIN": view_domain_url,
        "SSH_HOST": host_ip,
        "SSH_PRIVATE_KEY": ssh_key_path,
        "WEBSOCKET_URL": f"{ws_protocol}://{api_host}/ws",
        "API_URL": f"{protocol}://{api_host}/api",
        "WEBHOOK_URL": f"{protocol}://{api_host}/api/v1/webhook",
        "VIEW_DOMAIN": view_domain_url,
        # Better Auth CORS configuration - comma-separated list of allowed origins
        # Include auth origin if different from API (for IP-based setups with custom ports)
        "CORS_ALLOWED_ORIGINS": f"{view_domain_url},{api_domain_url},{auth_domain_url}" if auth_domain_url != api_domain_url else f"{view_domain_url},{api_domain_url}",
    }
    
    if better_auth_url is not None:
        # Ensure protocol is present (http:// or https://), add it based on secure flag
        if not better_auth_url.startswith(('http://', 'https://')):
            computed_auth_url = f"{protocol}://{better_auth_url}"
        else:
            computed_auth_url = better_auth_url
    else:
        # Use auth_host (which includes custom port if provided)
        computed_auth_url = auth_domain_url
    
    env_map["BETTER_AUTH_URL"] = computed_auth_url
    env_map["NEXT_PUBLIC_AUTH_URL"] = computed_auth_url
    
    if better_auth_secret is not None:
        env_map["BETTER_AUTH_SECRET"] = better_auth_secret
    
    return {k: v for k, v in env_map.items() if k in ENV_VAR_KEYS}


def update_environment_variables(
    env_values: dict,
    host_ip: str,
    api_domain: Optional[str],
    view_domain: Optional[str],
    ssh_key_path: str,
    caddy_http_port: Optional[int] = None,
    caddy_https_port: Optional[int] = None,
    api_port: Optional[int] = None,
    view_port: Optional[int] = None,
    auth_port: Optional[int] = None,
    db_port: Optional[int] = None,
    redis_port: Optional[int] = None,
    better_auth_url: Optional[str] = None,
    better_auth_secret: Optional[str] = None,
    external_db_url: Optional[str] = None,
    staging: bool = False,
) -> dict:
    updated_env = env_values.copy()
    
    if external_db_url:
        db_config = parse_db_url(external_db_url)
        updated_env.update(db_config)
        updated_env["DATABASE_URL"] = external_db_url
    else:
        updated_env["DATABASE_URL"] = construct_database_url(updated_env, staging=staging)
    
    updated_env["SECRET_MANAGER_ENABLED"] = "false"
    
    auth_service_url = get_auth_service_url(staging=staging)
    updated_env["AUTH_SERVICE_URL"] = f"http://{auth_service_url}"
    
    # BETTER_AUTH_URL should use internal Docker service URL for API service
    internal_auth_url = f"http://{auth_service_url}"
    updated_env["BETTER_AUTH_URL"] = internal_auth_url
    
    if not updated_env.get("HOST_NAME"):
        updated_env["HOST_NAME"] = get_db_service_name(staging)
    
    # Set DB_PORT if custom port provided (for docker-compose port mapping)
    # Note: DATABASE_URL uses internal Docker service name, not external port
    if db_port is not None and not updated_env.get("DB_PORT"):
        updated_env["DB_PORT"] = str(db_port)
    
    if not updated_env.get("REDIS_URL") or staging:
        redis_password = updated_env.get("REDIS_PASSWORD", "changeme")
        updated_env["REDIS_URL"] = get_redis_service_url(staging, redis_password)
    
    # Note: REDIS_URL uses internal Docker service name and port, not external port
    # External redis_port is only for docker-compose port mapping
    
    if not updated_env.get("CADDY_ENDPOINT"):
        updated_env["CADDY_ENDPOINT"] = get_caddy_endpoint(staging)
    
    env_map = build_env_variable_map(
        host_ip=host_ip,
        api_domain=api_domain,
        view_domain=view_domain,
        ssh_key_path=ssh_key_path,
        caddy_http_port=caddy_http_port,
        caddy_https_port=caddy_https_port,
        api_port=api_port,
        view_port=view_port,
        auth_port=auth_port,
        better_auth_url=better_auth_url,
        better_auth_secret=better_auth_secret,
    )

    for key, value in env_map.items():
        env_def = ENV_VAR_KEYS.get(key)
        if env_def and env_def.computed:
            # Don't override BETTER_AUTH_URL - we set it to internal Docker URL above
            # The public URL from build_env_variable_map is only for Better Auth service config
            if key == "BETTER_AUTH_URL":
                continue
            updated_env[key] = value
        elif key in updated_env:
            # Don't override BETTER_AUTH_URL - we set it to internal Docker URL above
            if key == "BETTER_AUTH_URL":
                continue
            updated_env[key] = value
    
    if staging and not external_db_url:
        updated_env["HOST_NAME"] = get_db_service_name(staging)
        updated_env["DATABASE_URL"] = construct_database_url(updated_env, staging=staging)
        # Always override REDIS_URL in staging to use correct service name
        redis_password = updated_env.get("REDIS_PASSWORD", "changeme")
        updated_env["REDIS_URL"] = get_redis_service_url(staging, redis_password)

    return updated_env


def setup_proxy_config(
    full_source_path: str,
    host_ip: str,
    view_domain: Optional[str],
    api_domain: Optional[str],
    staging: bool = False,
) -> str:
    caddy_json_template = os.path.join(full_source_path, "helpers", "caddy.json")
    
    with open(caddy_json_template, "r") as f:
        config_str = f.read()

    view_domain_or_ip = view_domain if view_domain is not None else host_ip
    api_domain_or_ip = api_domain if api_domain is not None else host_ip

    config_str = config_str.replace("{env.APP_DOMAIN}", view_domain_or_ip)
    config_str = config_str.replace("{env.API_DOMAIN}", api_domain_or_ip)

    app_reverse_proxy_url = get_view_service_url(staging=staging)
    api_reverse_proxy_url = get_api_service_url(staging=staging)
    config_str = config_str.replace("{env.APP_REVERSE_PROXY_URL}", app_reverse_proxy_url)
    config_str = config_str.replace("{env.API_REVERSE_PROXY_URL}", api_reverse_proxy_url)

    caddy_config = json.loads(config_str)
    
    # For IP-based deployments (no domains), disable automatic HTTPS
    # IP addresses cannot have SSL certificates, so HTTPS redirects won't work
    if view_domain is None and api_domain is None:
        if "apps" in caddy_config and "http" in caddy_config["apps"]:
            for server_name, server_config in caddy_config["apps"]["http"].get("servers", {}).items():
                if "automatic_https" in server_config:
                    server_config["automatic_https"]["disable"] = True
    
    with open(caddy_json_template, "w") as f:
        json.dump(caddy_config, f, indent=2)
    
    return caddy_json_template


def copy_caddyfile_to_target(full_source_path: str, config: dict, logger: Optional[LoggerProtocol] = None):
    try:
        source_caddyfile = os.path.join(full_source_path, "helpers", "Caddyfile")
        target_dir = get_config_value(config, CADDY_CONFIG_VOLUME)
        target_caddyfile = os.path.join(target_dir, "Caddyfile")
        create_directory(target_dir, logger=logger)
        if os.path.exists(source_caddyfile):
            shutil.copy2(source_caddyfile, target_caddyfile)
            set_permissions(target_caddyfile, 0o644, logger=logger)
            if logger:
                logger.debug(f"Copied Caddyfile from {source_caddyfile} to {target_caddyfile}")
        else:
            if logger:
                logger.warning(f"Source Caddyfile not found at {source_caddyfile}")
    except Exception as e:
        if logger:
            logger.error(f"Failed to copy Caddyfile: {str(e)}")


def get_access_url(
    view_domain: Optional[str], 
    api_domain: Optional[str], 
    host_ip: str, 
    caddy_http_port: Optional[int] = None,
    view_port: Optional[int] = None,
) -> str:
    if view_domain:
        return f"https://{view_domain}"
    elif api_domain:
        return f"https://{api_domain}"
    else:
        # For IP-based deployments, use view_port if provided, else fallback to caddy_http_port or default 7443
        # Note: IP addresses should use HTTP (not HTTPS) and direct service ports (not Caddy port)
        if view_port is not None:
            http_port = view_port
        elif caddy_http_port is not None:
            # Fallback to caddy_http_port for backward compatibility
            http_port = caddy_http_port
        else:
            # Default to view service default port
            http_port = 7443
        return f"http://{host_ip}:{http_port}"
