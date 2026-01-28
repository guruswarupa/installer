import os
from typing import Optional, Tuple

from app.commands.conf.conf import write_env_file
from app.utils.config import (
    API_ENV_FILE,
    API_PORT,
    BETTER_AUTH_SECRET,
    BETTER_AUTH_URL,
    CADDY_ADMIN_PORT,
    CADDY_HTTP_PORT,
    CADDY_HTTPS_PORT,
    DEFAULT_BRANCH,
    DEFAULT_REPO,
    AUTH_ENV_FILE,
    PROXY_PORT,
    VIEW_ENV_FILE,
    VIEW_PORT,
    get_config_value,
    get_service_env_values,
)
from app.utils.directory_manager import create_directory
from app.utils.file_manager import get_directory_path, set_permissions
from app.utils.protocols import LoggerProtocol

from .config_utils import (
    get_compose_file_path,
    get_full_source_path,
    get_host_ip_or_default,
    get_proxy_port,
    get_ssh_key_path,
    is_custom_repo_or_branch,
    parse_db_url,
    update_environment_variables,
)


class ConfigResolver:
    def __init__(
        self,
        config: dict,
        repo: Optional[str] = None,
        branch: Optional[str] = None,
        caddy_admin_port: Optional[int] = None,
        caddy_http_port: Optional[int] = None,
        caddy_https_port: Optional[int] = None,
        api_port: Optional[int] = None,
        view_port: Optional[int] = None,
        auth_port: Optional[int] = None,
        db_port: Optional[int] = None,
        redis_port: Optional[int] = None,
        better_auth_url: Optional[str] = None,
        better_auth_secret: Optional[str] = None,
        staging: bool = False,
    ):
        self.config = config
        self.repo = repo
        self.branch = branch
        self.caddy_admin_port = caddy_admin_port
        self.caddy_http_port = caddy_http_port
        self.caddy_https_port = caddy_https_port
        self.api_port = api_port
        self.view_port = view_port
        self.auth_port = auth_port
        self.db_port = db_port
        self.redis_port = redis_port
        self.better_auth_url = better_auth_url
        self.better_auth_secret = better_auth_secret
        self.staging = staging

    def get(self, path: str) -> str:
        if path == DEFAULT_REPO and self.repo is not None:
            return self.repo
        if path == DEFAULT_BRANCH and self.branch is not None:
            return self.branch

        if path == "full_source_path":
            return get_full_source_path(self.config)

        if path == "ssh_key_path":
            return get_ssh_key_path(self.config)

        if path == "compose_file_path":
            return get_compose_file_path(self.config, use_staging=self.staging)
        
        if path == PROXY_PORT:
            return str(get_proxy_port(self.config, self.caddy_admin_port))
        
        if path == CADDY_ADMIN_PORT and self.caddy_admin_port is not None:
            return str(self.caddy_admin_port)
        if path == CADDY_HTTP_PORT and self.caddy_http_port is not None:
            return str(self.caddy_http_port)
        if path == CADDY_HTTPS_PORT and self.caddy_https_port is not None:
            return str(self.caddy_https_port)
        if path == BETTER_AUTH_URL and self.better_auth_url is not None:
            return self.better_auth_url
        if path == BETTER_AUTH_SECRET and self.better_auth_secret is not None:
            return self.better_auth_secret

        return str(get_config_value(self.config, path))


def create_env_file_with_permissions(
    env_file: str,
    env_values: dict,
    logger: Optional[LoggerProtocol] = None,
) -> Tuple[bool, Optional[str]]:
    success, error = write_env_file(env_file, env_values, logger)
    if not success:
        return False, error
    
    file_perm_success, file_perm_error = set_permissions(env_file, 0o644)
    if not file_perm_success:
        return False, file_perm_error
    
    return True, None


def create_service_env_files(
    config: dict,
    config_resolver: ConfigResolver,
    host_ip: str,
    api_domain: Optional[str],
    view_domain: Optional[str],
    api_port: Optional[int] = None,
    view_port: Optional[int] = None,
    auth_port: Optional[int] = None,
    db_port: Optional[int] = None,
    redis_port: Optional[int] = None,
    external_db_url: Optional[str] = None,
    logger: Optional[LoggerProtocol] = None,
) -> Tuple[bool, Optional[str]]:
    api_env_file = config_resolver.get(API_ENV_FILE)
    view_env_file = config_resolver.get(VIEW_ENV_FILE)
    auth_env_file = config_resolver.get(AUTH_ENV_FILE)
    full_source_path = config_resolver.get("full_source_path")
    combined_env_file = os.path.join(full_source_path, ".env")
    
    create_directory(get_directory_path(api_env_file), logger=logger)
    create_directory(get_directory_path(view_env_file), logger=logger)
    create_directory(get_directory_path(auth_env_file), logger=logger)
    create_directory(get_directory_path(combined_env_file), logger=logger)

    services = [
        ("api", "services.api.env", api_env_file),
        ("view", "services.view.env", view_env_file),
        ("auth", "services.auth.env", auth_env_file),
    ]
    
    better_auth_url_val = config_resolver.better_auth_url if config_resolver.better_auth_url is not None else (config_resolver.get(BETTER_AUTH_URL) if config_resolver.get(BETTER_AUTH_URL) != "" else None)
    better_auth_secret_val = config_resolver.better_auth_secret if config_resolver.better_auth_secret is not None else (config_resolver.get(BETTER_AUTH_SECRET) if config_resolver.get(BETTER_AUTH_SECRET) != "" else None)
    
    for service_name, service_key, env_file in services:
        env_values = get_service_env_values(config, service_key)
        updated_env_values = update_environment_variables(
            env_values,
            host_ip,
            api_domain,
            view_domain,
            config_resolver.get("ssh_key_path"),
            caddy_http_port=config_resolver.caddy_http_port,
            caddy_https_port=config_resolver.caddy_https_port,
            api_port=api_port or config_resolver.api_port,
            view_port=view_port or config_resolver.view_port,
            auth_port=auth_port or config_resolver.auth_port,
            db_port=db_port or config_resolver.db_port,
            redis_port=redis_port or config_resolver.redis_port,
            better_auth_url=better_auth_url_val,
            better_auth_secret=better_auth_secret_val,
            external_db_url=external_db_url,
            staging=config_resolver.staging,
        )
        # TODO: Log staging values before writing (remove after debugging)
        if logger and config_resolver.staging:
            logger.debug(f"Staging mode: HOST_NAME={updated_env_values.get('HOST_NAME')}, REDIS_URL={updated_env_values.get('REDIS_URL')}, CADDY_ENDPOINT={updated_env_values.get('CADDY_ENDPOINT')}")
        
        success, error = create_env_file_with_permissions(env_file, updated_env_values, logger)
        if not success:
            return False, f"Failed to create {service_name} env file: {error}"
        if logger:
            logger.debug(f"Created {service_name} env file: {env_file}")

    api_env_values = get_service_env_values(config, "services.api.env")
    view_env_values = get_service_env_values(config, "services.view.env")

    combined_env_values = {}
    better_auth_url_val = config_resolver.better_auth_url if config_resolver.better_auth_url is not None else (config_resolver.get(BETTER_AUTH_URL) if config_resolver.get(BETTER_AUTH_URL) != "" else None)
    better_auth_secret_val = config_resolver.better_auth_secret if config_resolver.better_auth_secret is not None else (config_resolver.get(BETTER_AUTH_SECRET) if config_resolver.get(BETTER_AUTH_SECRET) != "" else None)
    combined_env_values.update(update_environment_variables(
        api_env_values,
        host_ip,
        api_domain,
        view_domain,
        config_resolver.get("ssh_key_path"),
        caddy_http_port=config_resolver.caddy_http_port,
        caddy_https_port=config_resolver.caddy_https_port,
        api_port=api_port or config_resolver.api_port,
        view_port=view_port or config_resolver.view_port,
        auth_port=auth_port or config_resolver.auth_port,
        db_port=db_port or config_resolver.db_port,
        redis_port=redis_port or config_resolver.redis_port,
        better_auth_url=better_auth_url_val,
        better_auth_secret=better_auth_secret_val,
        external_db_url=external_db_url,
        staging=config_resolver.staging,
    ))
    combined_env_values.update(update_environment_variables(
        view_env_values,
        host_ip,
        api_domain,
        view_domain,
        config_resolver.get("ssh_key_path"),
        caddy_http_port=config_resolver.caddy_http_port,
        caddy_https_port=config_resolver.caddy_https_port,
        api_port=api_port or config_resolver.api_port,
        view_port=view_port or config_resolver.view_port,
        auth_port=auth_port or config_resolver.auth_port,
        db_port=db_port or config_resolver.db_port,
        redis_port=redis_port or config_resolver.redis_port,
        better_auth_url=better_auth_url_val,
        better_auth_secret=better_auth_secret_val,
        external_db_url=external_db_url,
        staging=config_resolver.staging,
    ))
    success, error = create_env_file_with_permissions(combined_env_file, combined_env_values, logger)
    if not success:
        return False, f"Failed to create combined env file: {error}"
    if logger:
        logger.debug(f"Created combined env file: {combined_env_file}")
    
    return True, None

