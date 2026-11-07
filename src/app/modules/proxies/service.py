import asyncio
import json
import socket
import time
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
import socks
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import decrypt_text, encrypt_text
from app.core.errors import bad_request
from app.modules.proxies.models import Proxy, ProxyCheck
from app.modules.proxies.schemas import (
    ProxyBulkCheckRequest,
    ProxyBulkCheckResponse,
    ProxyCreate,
    ProxyParsedItem,
    ProxyUpdate,
)
from app.modules.telegram_accounts.models import TelegramAccount

PROXY_CHECK_TIMEOUT_SECONDS = 10
PROXY_BULK_MAX_ITEMS = 1_000
TELEGRAM_CONNECT_TARGETS = (
    ("192.0.2.50", 443),
    ("198.51.100.50", 443),
    ("api.telegram.org", 443),
)


def time_monotonic_ms() -> int:
    return int(time.monotonic() * 1000)


@dataclass(frozen=True)
class ParsedProxy:
    raw: str
    type: str
    host: str
    port: int
    username: str | None = None
    password: str | None = None


def _proxy_url_from_values(
    proxy_type: str,
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
) -> str:
    auth = ""
    if username:
        auth = f"{username}:{password or ''}@"
    return f"{proxy_type}://{auth}{host}:{port}"


def _proxy_url(payload: ProxyCreate) -> str:
    return _proxy_url_from_values(payload.type, payload.host, payload.port, payload.username, payload.password)


async def create_proxy(session: AsyncSession, workspace_id: str, payload: ProxyCreate) -> Proxy:
    proxy = Proxy(
        workspace_id=workspace_id,
        type=payload.type,
        host=payload.host,
        port=payload.port,
        encrypted_username=encrypt_text(payload.username),
        encrypted_password=encrypt_text(payload.password),
        encrypted_url=encrypt_text(_proxy_url(payload)),
        country=payload.country,
        provider=payload.provider,
        city_state=payload.city_state,
        proxy_class=payload.proxy_class or "unknown",
        exclusivity=payload.exclusivity or "unknown",
        sticky_until=payload.sticky_until,
        max_accounts=payload.max_accounts,
        note=payload.note,
    )
    session.add(proxy)
    await session.commit()
    await session.refresh(proxy)
    return proxy


async def create_proxy_records_from_raw_text(
    session: AsyncSession,
    workspace_id: str,
    raw_text: str,
    *,
    default_type: str = "socks5",
    provider: str | None = None,
    max_accounts: int = 1,
    note: str | None = None,
) -> list[Proxy]:
    parsed_items = parse_proxy_list(raw_text, default_type=default_type)
    parsed_proxies: list[ParsedProxy] = []
    invalid_items: list[ProxyParsedItem] = []
    for item in parsed_items:
        if isinstance(item, ProxyParsedItem):
            invalid_items.append(item)
        else:
            parsed_proxies.append(item)

    if invalid_items:
        first = invalid_items[0]
        raise bad_request(f"Не удалось распознать прокси '{first.raw}': {first.error_message or 'неверный формат'}")
    if not parsed_proxies:
        return []

    proxies: list[Proxy] = []
    for parsed in parsed_proxies:
        proxy = await _find_existing_proxy(session, workspace_id, parsed)
        if proxy:
            proxy.encrypted_username = encrypt_text(parsed.username)
            proxy.encrypted_password = encrypt_text(parsed.password)
            proxy.encrypted_url = encrypt_text(
                _proxy_url_from_values(parsed.type, parsed.host, parsed.port, parsed.username, parsed.password)
            )
            proxy.provider = provider or proxy.provider
            proxy.max_accounts = max_accounts
            proxy.note = note or proxy.note
            proxy.proxy_class = proxy.proxy_class or "unknown"
            proxy.exclusivity = proxy.exclusivity or "unknown"
            if proxy.status == "deleted":
                proxy.status = "new"
        else:
            proxy = Proxy(
                workspace_id=workspace_id,
                type=parsed.type,
                host=parsed.host,
                port=parsed.port,
                encrypted_username=encrypt_text(parsed.username),
                encrypted_password=encrypt_text(parsed.password),
                encrypted_url=encrypt_text(
                    _proxy_url_from_values(parsed.type, parsed.host, parsed.port, parsed.username, parsed.password)
                ),
                status="new",
                provider=provider,
                proxy_class="unknown",
                exclusivity="unknown",
                max_accounts=max_accounts,
                note=note,
            )
            session.add(proxy)
        await session.flush()
        proxies.append(proxy)
    return proxies


async def update_proxy(session: AsyncSession, proxy: Proxy, payload: ProxyUpdate) -> Proxy:
    data = payload.model_dump(exclude_unset=True)
    if "max_accounts" in data:
        current_accounts = await active_proxy_account_count(session, proxy.workspace_id, proxy.id)
        if int(data["max_accounts"]) < current_accounts:
            raise bad_request(
                f"Нельзя поставить max_accounts={data['max_accounts']}: "
                f"на прокси уже назначено {current_accounts} активных аккаунтов."
            )
    for field in [
        "type",
        "host",
        "port",
        "country",
        "provider",
        "city_state",
        "proxy_class",
        "exclusivity",
        "sticky_until",
        "max_accounts",
        "note",
    ]:
        if field in data:
            setattr(proxy, field, data[field])

    if "username" in data:
        proxy.encrypted_username = encrypt_text(data["username"])
    if "password" in data:
        proxy.encrypted_password = encrypt_text(data["password"])

    if {"type", "host", "port", "username", "password"} & data.keys():
        username = data.get("username")
        password = data.get("password")
        if "username" not in data:
            username = _decrypt_proxy_secret(proxy.encrypted_username)
        if "password" not in data:
            password = _decrypt_proxy_secret(proxy.encrypted_password)
        proxy.encrypted_url = encrypt_text(
            _proxy_url_from_values(proxy.type, proxy.host, proxy.port, username, password)
        )

    await session.commit()
    await session.refresh(proxy)
    return proxy


async def active_proxy_account_count(
    session: AsyncSession,
    workspace_id: str,
    proxy_id: str,
    *,
    exclude_account_id: str | None = None,
) -> int:
    query = select(func.count(TelegramAccount.id)).where(
        TelegramAccount.workspace_id == workspace_id,
        TelegramAccount.proxy_id == proxy_id,
        TelegramAccount.status.notin_(["archived", "deleted"]),
    )
    if exclude_account_id:
        query = query.where(TelegramAccount.id != exclude_account_id)
    result = await session.execute(query)
    return int(result.scalar_one() or 0)


async def ensure_proxy_account_capacity(
    session: AsyncSession,
    workspace_id: str,
    proxy: Proxy,
    *,
    exclude_account_id: str | None = None,
) -> None:
    if proxy.status not in {"active", "new"}:
        raise bad_request(
            f"Прокси {proxy.host}:{proxy.port} имеет статус {proxy.status}. Выберите новый чистый прокси."
        )
    assigned = await active_proxy_account_count(
        session,
        workspace_id,
        proxy.id,
        exclude_account_id=exclude_account_id,
    )
    if assigned >= proxy.max_accounts:
        raise bad_request(
            f"Прокси {proxy.host}:{proxy.port} уже достиг лимита {assigned}/{proxy.max_accounts} аккаунтов."
        )


def _decrypt_proxy_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return decrypt_text(value)
    except ValueError:
        return None


async def create_proxy_check(
    session: AsyncSession,
    workspace_id: str,
    proxy: Proxy,
    telegram_account_id: str | None = None,
) -> ProxyCheck:
    username = _decrypt_proxy_secret(proxy.encrypted_username)
    password = _decrypt_proxy_secret(proxy.encrypted_password)
    result = await check_proxy_values(
        proxy_type=proxy.type,
        host=proxy.host,
        port=proxy.port,
        username=username,
        password=password,
        check_telegram=True,
    )

    check = ProxyCheck(
        workspace_id=workspace_id,
        proxy_id=proxy.id,
        telegram_account_id=telegram_account_id,
        status=result["status"],
        latency_ms=result["latency_ms"],
        telegram_latency_ms=result["telegram_latency_ms"],
        ip_detected=result["ip_detected"],
        geo_country=result["geo_country"],
        error_code=result["error_code"],
        error_message=result["error_message"],
    )
    proxy.status = "active" if result["status"] == "succeeded" else "failed"
    proxy.country = result["geo_country"] or proxy.country
    proxy.last_ip_detected = result["ip_detected"] or proxy.last_ip_detected
    proxy.last_country_detected = result["geo_country"] or proxy.last_country_detected
    proxy.telegram_connectivity_status = _telegram_connectivity_status(result)
    proxy.last_error_code = result["error_code"]
    proxy.last_error_message = result["error_message"]
    session.add(check)
    await session.commit()
    await session.refresh(check)
    check.telegram_ok = result["telegram_ok"]  # type: ignore[attr-defined]
    return check


async def bulk_check_proxies(
    session: AsyncSession,
    workspace_id: str,
    payload: ProxyBulkCheckRequest,
) -> ProxyBulkCheckResponse:
    parsed_items = parse_proxy_list(payload.raw_text, default_type=payload.default_type)
    response_items: list[ProxyParsedItem] = []
    saved = 0
    valid = 0
    for parsed in parsed_items:
        if isinstance(parsed, ProxyParsedItem):
            response_items.append(parsed)
            continue

        result = await check_proxy_values(
            proxy_type=parsed.type,
            host=parsed.host,
            port=parsed.port,
            username=parsed.username,
            password=parsed.password,
            check_telegram=payload.check_telegram,
        )
        is_valid = result["status"] == "succeeded"
        if is_valid:
            valid += 1
        proxy_id = None
        if payload.save_valid and is_valid:
            proxy = await _find_existing_proxy(session, workspace_id, parsed)
            if proxy:
                proxy.encrypted_username = encrypt_text(parsed.username)
                proxy.encrypted_password = encrypt_text(parsed.password)
                proxy.encrypted_url = encrypt_text(
                    _proxy_url_from_values(parsed.type, parsed.host, parsed.port, parsed.username, parsed.password)
                )
                proxy.status = "active"
                proxy.country = result["geo_country"] or proxy.country
                proxy.provider = payload.provider or proxy.provider
                proxy.proxy_class = payload.proxy_class or proxy.proxy_class or "unknown"
                proxy.exclusivity = payload.exclusivity or proxy.exclusivity or "unknown"
                proxy.max_accounts = payload.max_accounts
                proxy.note = payload.note or proxy.note
                proxy.last_ip_detected = result["ip_detected"] or proxy.last_ip_detected
                proxy.last_country_detected = result["geo_country"] or proxy.last_country_detected
                proxy.telegram_connectivity_status = _telegram_connectivity_status(result)
                proxy.last_error_code = result["error_code"]
                proxy.last_error_message = result["error_message"]
            else:
                proxy = Proxy(
                    workspace_id=workspace_id,
                    type=parsed.type,
                    host=parsed.host,
                    port=parsed.port,
                    encrypted_username=encrypt_text(parsed.username),
                    encrypted_password=encrypt_text(parsed.password),
                    encrypted_url=encrypt_text(
                        _proxy_url_from_values(parsed.type, parsed.host, parsed.port, parsed.username, parsed.password)
                    ),
                    status="active",
                    country=result["geo_country"],
                    provider=payload.provider,
                    proxy_class=payload.proxy_class or "unknown",
                    exclusivity=payload.exclusivity or "unknown",
                    last_ip_detected=result["ip_detected"],
                    last_country_detected=result["geo_country"],
                    telegram_connectivity_status=_telegram_connectivity_status(result),
                    max_accounts=payload.max_accounts,
                    note=payload.note,
                    last_error_code=result["error_code"],
                    last_error_message=result["error_message"],
                )
                session.add(proxy)
            await session.flush()
            session.add(
                ProxyCheck(
                    workspace_id=workspace_id,
                    proxy_id=proxy.id,
                    status=result["status"],
                    latency_ms=result["latency_ms"],
                    telegram_latency_ms=result["telegram_latency_ms"],
                    ip_detected=result["ip_detected"],
                    geo_country=result["geo_country"],
                    error_code=result["error_code"],
                    error_message=result["error_message"],
                )
            )
            proxy_id = proxy.id
            saved += 1

        response_items.append(
            ProxyParsedItem(
                raw=parsed.raw,
                type=parsed.type,
                host=parsed.host,
                port=parsed.port,
                username=parsed.username,
                country=result["geo_country"],
                status=result["status"],
                latency_ms=result["latency_ms"],
                telegram_ok=result["telegram_ok"],
                telegram_latency_ms=result["telegram_latency_ms"],
                error_code=result["error_code"],
                error_message=result["error_message"],
                proxy_id=proxy_id,
            )
        )
    if saved:
        await session.commit()
    return ProxyBulkCheckResponse(
        total=len(response_items),
        parsed=sum(1 for item in response_items if item.host and item.port),
        valid=valid,
        saved=saved,
        items=response_items,
    )


def _telegram_connectivity_status(result: dict) -> str:
    if result.get("telegram_ok") is True:
        return "ok"
    if result.get("telegram_ok") is False:
        return "failed"
    return "skipped"


async def _find_existing_proxy(session: AsyncSession, workspace_id: str, parsed: ParsedProxy) -> Proxy | None:
    result = await session.execute(
        select(Proxy)
        .where(
            Proxy.workspace_id == workspace_id,
            Proxy.type == parsed.type,
            Proxy.host == parsed.host,
            Proxy.port == parsed.port,
            Proxy.status != "deleted",
        )
        .order_by(Proxy.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def parse_proxy_list(raw_text: str, *, default_type: str = "socks5") -> list[ParsedProxy | ProxyParsedItem]:
    chunks = [
        chunk.strip()
        for chunk in raw_text.replace("\r", "\n").replace(",", "\n").replace(";", "\n").splitlines()
        if chunk.strip()
    ][:PROXY_BULK_MAX_ITEMS]
    result: list[ParsedProxy | ProxyParsedItem] = []
    for raw in chunks:
        try:
            result.append(parse_proxy_line(raw, default_type=default_type))
        except ValueError as exc:
            result.append(
                ProxyParsedItem(raw=raw, status="invalid", error_code="PROXY_PARSE_FAILED", error_message=str(exc))
            )
    return result


def parse_proxy_line(raw: str, *, default_type: str = "socks5") -> ParsedProxy:
    value = raw.strip()
    proxy_type = default_type
    if "://" in value:
        parsed = urlparse(value)
        if parsed.scheme not in {"http", "socks5"}:
            raise ValueError("Поддерживаются только HTTP и SOCKS5.")
        if not parsed.hostname or not parsed.port:
            raise ValueError("Не удалось распознать host и port.")
        return ParsedProxy(
            raw=raw,
            type=parsed.scheme,
            host=parsed.hostname,
            port=parsed.port,
            username=parsed.username,
            password=parsed.password,
        )

    if "@" in value:
        auth, address = value.rsplit("@", 1)
        username, password = _split_auth(auth)
        host, port = _split_host_port(address)
        return ParsedProxy(raw=raw, type=proxy_type, host=host, port=port, username=username, password=password)

    parts = value.split(":")
    if len(parts) == 2:
        host, port = _split_host_port(value)
        return ParsedProxy(raw=raw, type=proxy_type, host=host, port=port)
    if len(parts) == 4:
        host, port = _split_host_port(":".join(parts[:2]))
        return ParsedProxy(raw=raw, type=proxy_type, host=host, port=port, username=parts[2], password=parts[3])
    raise ValueError("Формат должен быть host:port, host:port:user:pass или socks5://user:pass@host:port.")


def _split_host_port(value: str) -> tuple[str, int]:
    host, raw_port = value.rsplit(":", 1)
    host = host.strip()
    if not host:
        raise ValueError("Host пустой.")
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise ValueError("Port должен быть числом.") from exc
    if port < 1 or port > 65535:
        raise ValueError("Port должен быть от 1 до 65535.")
    return host, port


def _split_auth(value: str) -> tuple[str | None, str | None]:
    if ":" not in value:
        return value or None, None
    username, password = value.split(":", 1)
    return username or None, password or None


async def check_proxy_values(
    *,
    proxy_type: str,
    host: str,
    port: int,
    username: str | None = None,
    password: str | None = None,
    check_telegram: bool = True,
) -> dict[str, object]:
    tcp = await _check_tcp(host, port)
    if tcp["status"] != "succeeded":
        geo_country = await _proxy_geo_country(host)
        return {
            **tcp,
            "telegram_ok": False if check_telegram else None,
            "telegram_latency_ms": None,
            "geo_country": geo_country,
        }
    proxy_geo = await _proxy_geo_country_through_proxy(proxy_type, host, port, username, password)
    geo_country = proxy_geo["country"] or await _proxy_geo_country(host)
    ip_detected = proxy_geo["ip"]
    telegram_ok = None
    telegram_latency_ms = None
    error_code = tcp["error_code"]
    error_message = tcp["error_message"]
    if check_telegram:
        telegram = await _check_telegram_connect(proxy_type, host, port, username, password)
        telegram_ok = bool(telegram["ok"])
        telegram_latency_ms = telegram["latency_ms"]
        if not telegram_ok:
            error_code = telegram["error_code"]
            error_message = telegram["error_message"]
    status = "succeeded" if tcp["status"] == "succeeded" and (telegram_ok is not False) else "failed"
    return {
        "status": status,
        "latency_ms": tcp["latency_ms"],
        "ip_detected": ip_detected,
        "telegram_ok": telegram_ok,
        "telegram_latency_ms": telegram_latency_ms,
        "geo_country": geo_country,
        "error_code": error_code,
        "error_message": error_message,
    }


async def _check_tcp(host: str, port: int) -> dict[str, object]:
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        sock = await asyncio.wait_for(
            asyncio.to_thread(socket.create_connection, (host, port), 8),
            timeout=PROXY_CHECK_TIMEOUT_SECONDS,
        )
        sock.close()
        return {
            "status": "succeeded",
            "latency_ms": int((loop.time() - started) * 1000),
            "ip_detected": None,
            "error_code": None,
            "error_message": None,
        }
    except TimeoutError:
        return {
            "status": "failed",
            "latency_ms": None,
            "ip_detected": None,
            "error_code": "PROXY_TIMEOUT",
            "error_message": "Proxy TCP connection timed out",
        }
    except OSError as exc:
        return {
            "status": "failed",
            "latency_ms": None,
            "ip_detected": None,
            "error_code": "PROXY_CONNECT_FAILED",
            "error_message": str(exc),
        }


async def _check_telegram_connect(
    proxy_type: str,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
) -> dict[str, object]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_check_telegram_connect_sync, proxy_type, host, port, username, password),
            timeout=PROXY_CHECK_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        return {
            "ok": False,
            "latency_ms": None,
            "error_code": "TELEGRAM_CONNECT_FAILED",
            "error_message": str(exc),
        }


def _check_telegram_connect_sync(
    proxy_type: str,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
) -> dict[str, object]:
    proxy_kind = socks.SOCKS5 if proxy_type == "socks5" else socks.HTTP
    latencies: list[int] = []
    errors: list[str] = []
    checked = 0
    for target_host, target_port in TELEGRAM_CONNECT_TARGETS:
        checked += 1
        sock = socks.socksocket()
        sock.settimeout(PROXY_CHECK_TIMEOUT_SECONDS)
        sock.set_proxy(proxy_kind, host, port, rdns=True, username=username, password=password)
        started = time_monotonic_ms()
        try:
            sock.connect((target_host, target_port))
            latencies.append(time_monotonic_ms() - started)
        except Exception as exc:
            errors.append(f"{target_host}:{target_port} {type(exc).__name__}: {exc}")
        finally:
            sock.close()
    if latencies and len(latencies) == checked:
        return {
            "ok": True,
            "latency_ms": min(latencies),
            "error_code": None,
            "error_message": None,
        }
    if latencies and errors:
        return {
            "ok": False,
            "latency_ms": min(latencies),
            "error_code": "TELEGRAM_PARTIAL_CONNECT_FAILED",
            "error_message": "; ".join(errors),
        }
    return {
        "ok": False,
        "latency_ms": None,
        "error_code": "TELEGRAM_CONNECT_FAILED",
        "error_message": "; ".join(errors) or "Telegram endpoints are unavailable through proxy",
    }


async def _proxy_geo_country(host: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=4) as client:
            response = await client.get(f"http://ip-api.com/json/{host}", params={"fields": "status,countryCode"})
        data = response.json()
        if data.get("status") == "success" and data.get("countryCode"):
            return str(data["countryCode"]).upper()
    except Exception:
        return None
    return None


async def _proxy_geo_country_through_proxy(
    proxy_type: str,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
) -> dict[str, str | None]:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_proxy_geo_country_through_proxy_sync, proxy_type, host, port, username, password),
            timeout=PROXY_CHECK_TIMEOUT_SECONDS,
        )
    except Exception:
        return {"country": None, "ip": None}


def _proxy_geo_country_through_proxy_sync(
    proxy_type: str,
    host: str,
    port: int,
    username: str | None,
    password: str | None,
) -> dict[str, str | None]:
    proxy_kind = socks.SOCKS5 if proxy_type == "socks5" else socks.HTTP
    sock = socks.socksocket()
    sock.settimeout(PROXY_CHECK_TIMEOUT_SECONDS)
    sock.set_proxy(proxy_kind, host, port, rdns=True, username=username, password=password)
    try:
        sock.connect(("ip-api.com", 80))
        request = (
            "GET /json/?fields=status,query,countryCode HTTP/1.1\r\n"
            "Host: ip-api.com\r\n"
            "User-Agent: demo-platform-proxy-check/1.0\r\n"
            "Connection: close\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        chunks: list[bytes] = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
        _, _, body = b"".join(chunks).partition(b"\r\n\r\n")
        data = json.loads(body.decode("utf-8", errors="replace"))
        country = data.get("countryCode")
        ip = data.get("query")
        return {
            "country": str(country).upper() if country else None,
            "ip": str(ip) if ip else None,
        }
    finally:
        sock.close()
