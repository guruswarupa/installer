import os
import re
from typing import Any, Callable, List, Optional, Tuple

import typer
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn

from app.commands.clone.clone import clone_repository
from app.commands.preflight.preflight import check_required_ports
from app.utils.config import (
    API_PORT,
    CADDY_ADMIN_PORT,
    CADDY_HTTP_PORT,
    CADDY_HTTPS_PORT,
    DEFAULT_BRANCH,
    DEFAULT_REPO,
    NIXOPUS_CONFIG_DIR,
    PORTS,
    PROXY_PORT,
    SSH_KEY_SIZE,
    SSH_KEY_TYPE,
    VIEW_PORT,
    get_active_config,
    get_config_value,
)
from app.utils.timeout import timeout_wrapper
from app.utils.installation_tracker import track_installation_failure, track_installation_success, track_staging_installation

from .admin_registration import register_admin_user_step
from .types import InstallParams
from .config_utils import (
    get_access_url,
    get_host_ip_or_default,
    is_custom_repo_or_branch,
)
from .deps import install_all_deps
from .environment import ConfigResolver, create_service_env_files
from .service_constants import INTERNAL_AUTH_PORT
from .messages import (
    clone_failed,
    dependency_installation_timeout,
    installation_failed,
    installing_nixopus,
    operation_timed_out,
    proxy_load_failed,
    root_required,
    services_start_failed,
    ssh_setup_failed,
)
from .services import (
    build_service_env_vars,
    cleanup_docker_services,
    load_proxy_config,
    setup_proxy_configuration,
    start_docker_services,
)
from .rollback import perform_installation_rollback
from .ssh import SSHConfig, generate_ssh_key_with_config
from .validate import validate_domains, validate_host_ip, validate_repo


def validate_install_params(params: InstallParams) -> None:
    validate_domains(params.api_domain, params.view_domain)
    validate_repo(params.repo)
    validate_host_ip(params.host_ip)


def create_config_resolver(config: dict, params: InstallParams) -> ConfigResolver:
    return ConfigResolver(
        config,
        repo=params.repo,
        branch=params.branch,
        caddy_admin_port=params.caddy_admin_port,
        caddy_http_port=params.caddy_http_port,
        caddy_https_port=params.caddy_https_port,
        api_port=params.api_port,
        view_port=params.view_port,
        auth_port=params.auth_port,
        db_port=params.db_port,
        redis_port=params.redis_port,
        better_auth_url=params.better_auth_url,
        better_auth_secret=params.better_auth_secret,
        staging=params.staging,
    )


def _extract_default_port_from_env_var(env_var_value: Any) -> Optional[int]:
    if not isinstance(env_var_value, str):
        return None
    
    match = re.search(r':-(\d+)', env_var_value)
    if match:
        try:
            return int(match.group(1))
        except ValueError:
            pass
    
    cleaned = re.sub(r'\$\{[^}]+\}', '', env_var_value).strip()
    if cleaned and cleaned.isdigit():
        try:
            return int(cleaned)
        except ValueError:
            pass
    
    return None


def _get_config_port_value(config: dict, port_path: str) -> Optional[int]:
    try:
        keys = port_path.split(".")
        value = config
        for key in keys:
            if isinstance(value, dict) and key in value:
                value = value[key]
            else:
                return None
        
        if not isinstance(value, str):
            return None
        
        return _extract_default_port_from_env_var(value)
    except (KeyError, ValueError, TypeError, AttributeError):
        return None


def build_ports_to_check(config: dict, params: InstallParams) -> List[int]:
    config_ports = get_config_value(config, PORTS)
    config_ports = [int(port) for port in config_ports] if isinstance(config_ports, list) else [int(config_ports)]
    config_caddy_admin_port = _get_config_port_value(config, CADDY_ADMIN_PORT)
    config_caddy_http_port = _get_config_port_value(config, CADDY_HTTP_PORT)
    config_caddy_https_port = _get_config_port_value(config, CADDY_HTTPS_PORT)
    
    port_overrides = {}
    if config_caddy_admin_port is not None and params.caddy_admin_port is not None:
        port_overrides[config_caddy_admin_port] = params.caddy_admin_port
    if config_caddy_http_port is not None and params.caddy_http_port is not None:
        port_overrides[config_caddy_http_port] = params.caddy_http_port
    if config_caddy_https_port is not None and params.caddy_https_port is not None:
        port_overrides[config_caddy_https_port] = params.caddy_https_port
    
    ports = []
    for config_port in config_ports:
        user_port = port_overrides.get(config_port)
        ports.append(user_port if user_port is not None else config_port)
    
    return ports


def run_preflight_checks(config: dict, params: InstallParams) -> None:
    ports = build_ports_to_check(config, params)
    check_required_ports(ports, logger=params.logger)


def install_dependencies(params: InstallParams) -> None:
    try:
        with timeout_wrapper(params.timeout):
            install_all_deps(verbose=params.verbose, output="json", dry_run=params.dry_run)
    except TimeoutError:
        raise Exception(dependency_installation_timeout)


def clone_repository_step(config_resolver: ConfigResolver, params: InstallParams) -> None:
    if params.dry_run:
        if params.logger:
            params.logger.info(
                f"[DRY RUN] Would clone {config_resolver.get(DEFAULT_REPO)} to {config_resolver.get('full_source_path')}"
            )
        return

    try:
        with timeout_wrapper(params.timeout):
            import sys
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
            
            success, error = clone_repository(
                repo=config_resolver.get(DEFAULT_REPO),
                path=config_resolver.get("full_source_path"),
                branch=config_resolver.get(DEFAULT_BRANCH),
                force=params.force,
                logger=params.logger,
                interactive=interactive,
            )
    except TimeoutError:
        raise Exception(f"{clone_failed}: {operation_timed_out}")
    
    if not success:
        raise Exception(f"{clone_failed}: {error}")


def clone_auth_service_step(config_resolver: ConfigResolver, params: InstallParams) -> None:
    nixopus_config_dir = config_resolver.get(NIXOPUS_CONFIG_DIR)
    auth_path = os.path.join(nixopus_config_dir, "auth")
    auth_repo = "https://github.com/nixopus/auth"
    
    if params.dry_run:
        if params.logger:
            params.logger.info(
                f"[DRY RUN] Would clone {auth_repo} to {auth_path}"
            )
        return

    try:
        with timeout_wrapper(params.timeout):
            import sys
            interactive = sys.stdin.isatty() and sys.stdout.isatty()
            
            success, error = clone_repository(
                repo=auth_repo,
                path=auth_path,
                branch=None,  # Use default branch
                force=params.force,
                logger=params.logger,
                interactive=interactive,
            )
    except TimeoutError:
        raise Exception(f"{clone_failed}: {operation_timed_out}")
    
    if not success:
        raise Exception(f"{clone_failed}: {error}")


def create_auth_service_env_step(config_resolver: ConfigResolver, params: InstallParams) -> None:
    from app.utils.directory_manager import create_directory
    from app.utils.file_manager import get_directory_path, set_permissions
    from app.commands.conf.conf import write_env_file
    from app.utils.config import get_service_env_values
    from .config_utils import construct_database_url
    
    nixopus_config_dir = config_resolver.get(NIXOPUS_CONFIG_DIR)
    auth_path = os.path.join(nixopus_config_dir, "auth")
    auth_env_file = os.path.join(auth_path, ".env")
    
    if params.dry_run:
        if params.logger:
            params.logger.info(
                f"[DRY RUN] Would create auth .env file at {auth_env_file}"
            )
        return
    
    create_directory(get_directory_path(auth_env_file), logger=params.logger)
    
    database_url = None
    if params.external_db_url:
        database_url = params.external_db_url
    else:
        api_env_values = get_service_env_values(config_resolver.config, "services.api.env")
        database_url = construct_database_url(api_env_values, staging=params.staging)
    
    # Compute frontend and backend origins for Better Auth trusted origins (CORS_ALLOWED_ORIGINS)
    host_ip = get_host_ip_or_default(params.host_ip)
    secure = params.api_domain is not None and params.view_domain is not None
    protocol = "https" if secure else "http"
    
    # Compute view (frontend) origin - use custom ports when not using domains
    if secure:
        view_host = params.view_domain
    else:
        # Use custom view_port if provided, else fallback to caddy_http_port, else default 7443
        view_port_final = params.view_port if params.view_port is not None else (params.caddy_http_port if params.caddy_http_port is not None else 7443)
        view_host = f"{host_ip}:{view_port_final}"
    view_domain = f"{protocol}://{view_host}"
    
    # Compute API (backend) origin - use custom ports when not using domains
    if secure:
        api_host = params.api_domain
    else:
        # Use custom api_port if provided, else fallback to caddy_http_port, else default 8443
        api_port_final = params.api_port if params.api_port is not None else (params.caddy_http_port if params.caddy_http_port is not None else 8443)
        api_host = f"{host_ip}:{api_port_final}"
    api_domain = f"{protocol}://{api_host}"
    
    # Compute Auth service origin - use custom ports when not using domains
    if secure:
        # For domains, auth host defaults to better_auth_url or api_domain
        auth_host = params.better_auth_url if params.better_auth_url else params.api_domain
        if auth_host and not auth_host.startswith(('http://', 'https://')):
            auth_host = f"{protocol}://{auth_host}"
    else:
        # Use custom auth_port if provided, else fallback to caddy_http_port, else default 9090
        auth_port_final = params.auth_port if params.auth_port is not None else (params.caddy_http_port if params.caddy_http_port is not None else 9090)
        auth_host = f"{host_ip}:{auth_port_final}"
    
    # Combine origins for CORS_ALLOWED_ORIGINS (comma-separated)
    # Include auth origin if different from API (for IP-based setups with custom ports)
    auth_domain = f"{protocol}://{auth_host}" if not auth_host.startswith(('http://', 'https://')) else auth_host
    if secure or auth_domain == api_domain:
        cors_allowed_origins = f"{view_domain},{api_domain}"
    else:
        cors_allowed_origins = f"{view_domain},{api_domain},{auth_domain}"
    
    # Extract base domain for cookie sharing across subdomains
    # Handles both root domains (e.g., "example.com") and subdomains (e.g., "view.example.com") - ".example.com"
    cookie_domain = None
    if secure and params.view_domain:
        domain_parts = params.view_domain.split('.')
        if len(domain_parts) >= 2:
            base_domain = '.'.join(domain_parts[-2:])
            cookie_domain = f".{base_domain}"  # Add leading dot for subdomain sharing
    
    better_auth_base_url = None
    if params.better_auth_url:
        better_auth_base_url = params.better_auth_url
        if not better_auth_base_url.startswith(('http://', 'https://')):
            better_auth_base_url = f"{protocol}://{better_auth_base_url}"
    elif secure:
        better_auth_base_url = api_domain
    else:
        # For IP-based setup, use auth_host we computed above
        auth_domain_url = f"{protocol}://{auth_host}"
        better_auth_base_url = auth_domain_url
    
    env_values = {
        "DATABASE_URL": database_url,
        "SECRET_MANAGER_ENABLED": "false",
        "PORT": str(INTERNAL_AUTH_PORT),
        "VIEW_DOMAIN": view_domain,
        "CORS_ALLOWED_ORIGINS": cors_allowed_origins,
        "BETTER_AUTH_BASE_URL": better_auth_base_url, 
        "BETTER_AUTH_URL": better_auth_base_url,  
    }
    
    # Cookie configuration for Better Auth
    if cookie_domain:
        env_values["BETTER_AUTH_COOKIE_DOMAIN"] = cookie_domain
    if secure:
        env_values["BETTER_AUTH_SECURE_COOKIES"] = "true"
    
    # Resend email configuration (optional)
    if params.resend_api_key:
        env_values["RESEND_API_KEY"] = params.resend_api_key
    if params.resend_from_email:
        env_values["RESEND_FROM_EMAIL"] = params.resend_from_email
    
    success, error = write_env_file(auth_env_file, env_values, params.logger)
    if not success:
        raise Exception(f"Failed to create auth .env file: {error}")
    
    file_perm_success, file_perm_error = set_permissions(auth_env_file, 0o644)
    if not file_perm_success:
        raise Exception(f"Failed to set permissions on auth .env file: {file_perm_error}")
    
    if params.logger:
        params.logger.debug(f"Created auth env file: {auth_env_file}")


def cleanup_docker_step(config_resolver: ConfigResolver, params: InstallParams) -> None:
    compose_file = config_resolver.get("compose_file_path")
    cleanup_docker_services(compose_file, params.dry_run, params.logger)


def create_env_files_step(config: dict, config_resolver: ConfigResolver, params: InstallParams) -> None:
    success, error = create_service_env_files(
        config,
        config_resolver,
        get_host_ip_or_default(params.host_ip),
        params.api_domain,
        params.view_domain,
        api_port=params.api_port,
        view_port=params.view_port,
        auth_port=params.auth_port,
        db_port=params.db_port,
        redis_port=params.redis_port,
        external_db_url=params.external_db_url,
        logger=params.logger,
    )
    if not success:
        raise Exception(error)


def setup_proxy_config_step(config: dict, config_resolver: ConfigResolver, params: InstallParams) -> None:
    # Skip Caddy setup for IP-based deployments (no domains)
    # Caddy is only needed for domain-based deployments with automatic HTTPS
    # IP addresses cannot have SSL certificates, so Caddy isn't needed
    if not params.view_domain and not params.api_domain:
        if params.logger:
            params.logger.debug("Skipping Caddy proxy setup for IP-based deployment (services accessed directly)")
        return
    
    full_source_path = config_resolver.get("full_source_path")
    setup_proxy_configuration(
        full_source_path,
        get_host_ip_or_default(params.host_ip),
        params.view_domain,
        params.api_domain,
        config,
        params.dry_run,
        staging=params.staging,
        logger=params.logger,
    )


def setup_ssh_step(config: dict, config_resolver: ConfigResolver, params: InstallParams) -> None:
    ssh_config = SSHConfig(
        path=config_resolver.get("ssh_key_path"),
        key_type=get_config_value(config, SSH_KEY_TYPE),
        key_size=get_config_value(config, SSH_KEY_SIZE),
        passphrase=None,
        verbose=params.verbose,
        output="text",
        dry_run=params.dry_run,
        force=params.force,
        set_permissions=True,
        add_to_authorized_keys=True,
        create_ssh_directory=True,
    )
    try:
        with timeout_wrapper(params.timeout):
            result = generate_ssh_key_with_config(ssh_config, logger=params.logger)
    except TimeoutError:
        raise Exception(f"{ssh_setup_failed}: {operation_timed_out}")
    
    if not result.success:
        raise Exception(ssh_setup_failed)


def start_services_step(config_resolver: ConfigResolver, params: InstallParams) -> None:
    compose_file = config_resolver.get("compose_file_path")
    env_vars = build_service_env_vars(
        params.caddy_admin_port,
        params.caddy_http_port,
        params.caddy_https_port,
        api_port=params.api_port,
        view_port=params.view_port,
        auth_port=params.auth_port,
        db_port=params.db_port,
        redis_port=params.redis_port,
    )
    
    profiles = [] if params.external_db_url else ["local-db"]
    
    success, error = start_docker_services(
        compose_file,
        env_vars,
        params.timeout,
        params.dry_run,
        params.logger,
        profiles=profiles,
        verify_health=params.verify_health,
        health_check_timeout=params.health_check_timeout,
    )
    if not success:
        raise Exception(f"{services_start_failed}: {error}")


def load_proxy_step(config_resolver: ConfigResolver, params: InstallParams) -> None:
    # Skip Caddy proxy loading for IP-based deployments (no domains)
    # Caddy is only needed for domain-based deployments with automatic HTTPS
    # IP addresses cannot have SSL certificates, so Caddy isn't needed
    if not params.view_domain and not params.api_domain:
        if params.logger:
            params.logger.debug("Skipping Caddy proxy loading for IP-based deployment (services accessed directly)")
        return
    
    proxy_port = config_resolver.get(PROXY_PORT)
    try:
        proxy_port = int(proxy_port)
    except (ValueError, TypeError):
        proxy_port = 2019

    full_source_path = config_resolver.get("full_source_path")
    caddy_json_config = os.path.join(full_source_path, "helpers", "caddy.json")

    success, error = load_proxy_config(
        caddy_json_config,
        proxy_port,
        params.timeout,
        params.dry_run,
        params.logger,
    )
    if not success:
        raise Exception(f"{proxy_load_failed}: {error}")


def build_installation_steps(
    config: dict,
    config_resolver: ConfigResolver,
    params: InstallParams,
) -> List[Tuple[str, Callable[[], None]]]:
    services_step_desc = "Starting services" if not params.verify_health else "Starting and verifying services"
    
    steps = [
        ("Preflight checks", lambda: run_preflight_checks(config, params)),
        ("Installing dependencies", lambda: install_dependencies(params)),
        ("Cloning repository", lambda: clone_repository_step(config_resolver, params)),
        ("Cloning auth repository", lambda: clone_auth_service_step(config_resolver, params)),
        ("Creating auth environment file", lambda: create_auth_service_env_step(config_resolver, params)),
        ("Setting up proxy config", lambda: setup_proxy_config_step(config, config_resolver, params)),
        ("Creating environment files", lambda: create_env_files_step(config, config_resolver, params)),
        ("Generating SSH keys", lambda: setup_ssh_step(config, config_resolver, params)),
        (services_step_desc, lambda: start_services_step(config_resolver, params)),
        ("Loading proxy configuration", lambda: load_proxy_step(config_resolver, params)),
    ]

    if params.force:
        steps.insert(2, ("Cleaning up Docker resources", lambda: cleanup_docker_step(config_resolver, params)))

    if (params.admin_email or params.admin_password) and params.verify_health:
        steps.append(("Registering admin user", lambda: register_admin_user_step(config_resolver, params)))

    return steps

def show_success_message(config_resolver: ConfigResolver, params: InstallParams) -> None:
    nixopus_accessible_at = get_access_url(
        params.view_domain,
        params.api_domain,
        get_host_ip_or_default(params.host_ip),
        caddy_http_port=params.caddy_http_port,
        view_port=params.view_port,
    )

    if params.logger:
        params.logger.success("Installation Complete!")
        params.logger.info(f"Nixopus is accessible at: {nixopus_accessible_at}")
        params.logger.highlight("Thank you for installing Nixopus!")
        params.logger.info("Please visit the documentation at https://docs.nixopus.com for more information.")
        params.logger.info("If you have any questions, please visit the community forum at https://discord.gg/skdcq39Wpv")
        params.logger.highlight("See you in the community!")
    
    try:
        track_installation_success(staging=params.staging, logger=params.logger)
    except Exception:
        pass


def handle_installation_error(error: Exception, params: InstallParams, context: str = "") -> None:  
    if not params.logger:
        return
    
    context_msg = f" during {context}" if context else ""
    if params.verbose:
        params.logger.error(f"{installation_failed}{context_msg}: {str(error)}")
    else:
        params.logger.error(f"{installation_failed}{context_msg}")
    
    try:
        track_installation_failure(
            failed_step=context,
            staging=params.staging,
            error_message=str(error) if params.verbose else None,
            logger=params.logger,
        )
    except Exception:
        pass


def run_installation(params: InstallParams) -> None:
    if os.geteuid() != 0:
        if params.logger:
            params.logger.error(root_required)
        raise Exception(root_required)
    
    config = get_active_config(user_config_file=params.config_file)
    
    validate_install_params(params)
    
    if is_custom_repo_or_branch(params.repo, params.branch):
        if params.logger:
            compose_file = "docker-compose-staging.yml" if params.staging else "docker-compose.yml"
            params.logger.info(f"Custom repository/branch detected - will use {compose_file}")
    
    if params.staging:
        try:
            track_staging_installation(logger=params.logger)
        except Exception:
            pass
    
    config_resolver = create_config_resolver(config, params)
    steps = build_installation_steps(config, config_resolver, params)
    completed_steps = []
    failed_step = None

    try:
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            transient=True,
            refresh_per_second=2,
        ) as progress:
            main_task = progress.add_task(installing_nixopus, total=len(steps))

            for i, (step_name, step_func) in enumerate(steps):
                progress.update(main_task, description=f"{installing_nixopus} - {step_name} ({i+1}/{len(steps)})")
                try:
                    step_func()
                    completed_steps.append(step_name)
                    progress.advance(main_task, 1)
                except Exception as e:
                    progress.update(main_task, description=f"Failed at {step_name}")
                    failed_step = step_name
                    raise

            progress.update(main_task, completed=True, description="Installation completed")

        show_success_message(config_resolver, params)

    except Exception as e:
        handle_installation_error(e, params, failed_step or "")
        
        if not params.no_rollback and completed_steps:
            perform_installation_rollback(
                completed_steps,
                config_resolver,
                config,
                params.dry_run,
                params.logger,
            )
        
        if params.logger:
            params.logger.error(f"{installation_failed}: {str(e)}")
        raise typer.Exit(1)
