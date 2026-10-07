#!/usr/bin/env python3
"""primoChat OpenClaw + Kimi Code pilot installer. Python 3.10+, Linux/systemd.

Credentials are accepted only on a terminal. No shell, redirects, telemetry or
chat messages. Existing OpenClaw configuration is preserved outside the selected
model and Matrix channel. Run as the user who owns openclaw-gateway.service.
"""
import copy
from contextlib import contextmanager
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = "0.1.0"
ENROLL = "https://bots.primochat.ru/v1/native-matrix-agents/enroll"
HOMESERVER = "https://matrix.primochat.ru"
KIMI_BASE = "https://api.kimi.ai/coding/"
MODEL = "kimi/kimi-for-coding"
SERVICE = "openclaw-gateway.service"
SEARCH_IMAGE = "docker.io/searxng/searxng@sha256:76b0bf285aca014c7191fc4d9234c4bfb358624ac33d8883833d496c059ec072"
LIMIT = 256 * 1024


class SetupError(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise SetupError("Сервер перенаправил запрос. Настройка остановлена.")


def request_json(url, payload, headers=None):
    if url not in (ENROLL, KIMI_BASE + "v1/messages"):
        raise SetupError("Неподдерживаемый адрес сервера.")
    request = urllib.request.Request(url, json.dumps(payload).encode(),
                                     {"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=45) as response:
            raw = response.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise SetupError("Ответ сервера слишком большой.")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise ValueError()
        return result
    except urllib.error.HTTPError as error:
        if url == ENROLL:
            raise SetupError("Код подключения не принят. Выпустите новый код в Studio.") from None
        if error.code in (401, 403):
            raise SetupError("Kimi не принял ключ или доступ к модели. Проверьте ключ и подписку в kimi.ai/code/console.") from None
        raise SetupError("Kimi временно недоступен или исчерпан лимит. HTTP " + str(error.code)) from None
    except (OSError, ValueError):
        raise SetupError("Не удалось получить ответ. Проверьте сеть; использованный код подключения может потребовать перевыпуска.") from None


def command(args, data=None, timeout=60, check=True):
    try:
        result = subprocess.run(args, input=data, text=True, encoding='utf-8', errors='replace', stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise SetupError("Команда не завершилась: " + args[0]) from None
    if check and result.returncode:
        # CLI errors may echo configuration, credentials or provider responses.
        raise SetupError("Не выполнен шаг: " + " ".join(args[:3]) + ". Вывод скрыт, чтобы не раскрыть ключи.")
    return result


def write_private(path, value):
    raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False, indent=2).encode()
    fd, name = tempfile.mkstemp(prefix=".setup-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_enrollment(data, owner):
    if set(data) != {"provider", "homeserverUrl", "botMatrixId", "ownerMatrixId", "roomId", "accessToken"}:
        raise SetupError("Неверный ответ подключения.")
    if (data["provider"] != "openclaw" or data["homeserverUrl"].rstrip("/") != HOMESERVER
            or data["ownerMatrixId"] != owner
            or not re.fullmatch(r"@openclaw_[a-f0-9]{12}_bot:primochat\.ru", data["botMatrixId"])
            or not re.fullmatch(r"![^\s:]+:primochat\.ru", data["roomId"])
            or not isinstance(data["accessToken"], str) or not 16 <= len(data["accessToken"]) <= 4096):
        raise SetupError("Подключение относится к другому аккаунту или серверу. Изменения не применены.")
    return data


def configure(original, enrollment, secrets_path, search_base=None):
    result = copy.deepcopy(original)
    providers = result.setdefault("secrets", {}).setdefault("providers", {})
    if "primo_setup" in providers:
        previous = providers["primo_setup"]
        if (previous.get("source") != "file" or previous.get("mode") != "json"
                or not Path(previous.get("path", "")).is_relative_to(secrets_path.parent.parent)):
            raise SetupError("Имя хранилища primo_setup уже занято.")
    providers["primo_setup"] = {"source": "file", "path": str(secrets_path), "mode": "json"}
    kimi = result.setdefault("models", {}).setdefault("providers", {}).setdefault("kimi", {})
    kimi.update({"baseUrl": KIMI_BASE, "api": "anthropic-messages", "auth": "api-key",
                 "apiKey": {"source": "file", "provider": "primo_setup", "id": "/kimiApiKey"}})
    # Explicit catalog avoids dependence on an already installed Kimi plugin.
    kimi["models"] = [{"id": "kimi-for-coding", "name": "Kimi Code", "reasoning": True,
                       "input": ["text", "image"], "contextWindow": 262144, "maxTokens": 32768}]
    defaults = result.setdefault("agents", {}).setdefault("defaults", {})
    defaults["model"] = {"primary": MODEL, "fallbacks": []}
    defaults.setdefault("models", {})[MODEL] = {"alias": "Kimi"}
    main_agent = result["agents"].get("entries", {}).get("main")
    if main_agent is not None:
        main_agent["model"] = {"primary": MODEL, "fallbacks": []}
    result.setdefault("channels", {})["matrix"] = {
        "enabled": True, "homeserver": HOMESERVER, "userId": enrollment["botMatrixId"],
        "accessToken": {"source": "file", "provider": "primo_setup", "id": "/matrixAccessToken"},
        "encryption": False, "dm": {"policy": "disabled", "allowFrom": []},
        "groupPolicy": "allowlist", "groupAllowFrom": [enrollment["ownerMatrixId"]],
        "groups": {enrollment["roomId"]: {"requireMention": False}},
        "autoJoin": "allowlist", "autoJoinAllowlist": [enrollment["roomId"]],
        "joinIntro": False, "threadReplies": "inbound",
    }
    if search_base:
        if not re.fullmatch(r'http://127\.0\.0\.1:[0-9]{1,5}',search_base):
            raise SetupError('Поиск должен быть доступен только локально.')
        result.setdefault('tools',{}).setdefault('web',{}).setdefault('search',{}).update({'provider':'searxng','enabled':True})
        result.setdefault('plugins',{}).setdefault('entries',{}).setdefault('searxng',{}).setdefault('config',{}).setdefault('webSearch',{})['baseUrl']=search_base
    return result


def bundled_search(root):
    """One owned container per Linux user; never adopt an unrelated container."""
    name='primochat-openclaw-search-'+str(os.geteuid())
    label=str(root.resolve())
    inspected=command(['docker','container','inspect',name],check=False)
    if inspected.returncode:
        folder=root/'primochat-search'
        if folder.is_symlink(): raise SetupError('Небезопасный каталог поиска.')
        folder.mkdir(mode=0o700,exist_ok=True)
        os.chmod(folder,0o700)
        settings=folder/'settings.yml'
        if settings.is_symlink(): raise SetupError('Небезопасный файл настроек поиска.')
        if not settings.exists():
            content='use_default_settings: true\nserver:\n  secret_key: "'+os.urandom(32).hex()+'"\n  limiter: false\n  image_proxy: false\nsearch:\n  formats: [html, json]\n'
            write_private(settings,content.encode())
        command(['docker','pull',SEARCH_IMAGE],timeout=300)
        with socket.socket() as reserved:
            reserved.bind(('127.0.0.1',0))
            chosen_port=reserved.getsockname()[1]
        command(['docker','run','-d','--name',name,'--label','io.primochat.openclaw-search='+label,
                 '--restart','unless-stopped','--user',str(os.geteuid())+':'+str(os.getegid()),
                 '--cap-drop=ALL','--security-opt=no-new-privileges','--read-only',
                 '--memory=512m','--cpus=1','--pids-limit=128',
                 '--tmpfs','/tmp:rw,nosuid,nodev,size=64m','--tmpfs','/var/cache/searxng:rw,nosuid,nodev,size=64m,mode=1777',
                 '-e','GRANIAN_WORKERS=1','-e','GRANIAN_BLOCKING_THREADS=2',
                 '-p','127.0.0.1:'+str(chosen_port)+':8080','--mount','type=bind,src='+str(folder)+',dst=/etc/searxng,readonly',SEARCH_IMAGE],timeout=90)
        inspected=command(['docker','container','inspect',name])
    details=json.loads(inspected.stdout)[0]
    bindings=details.get('HostConfig',{}).get('PortBindings',{}).get('8080/tcp',[])
    if (details.get('Config',{}).get('Labels',{}).get('io.primochat.openclaw-search')!=label
            or details.get('Config',{}).get('Image')!=SEARCH_IMAGE
            or len(bindings)!=1 or bindings[0].get('HostIp')!='127.0.0.1'):
        raise SetupError('Контейнер с именем поиска уже занят другой конфигурацией. Он не изменён.')
    if not details.get('State',{}).get('Running'):
        command(['docker','start',name])
        details=json.loads(command(['docker','container','inspect',name]).stdout)[0]
    port=details['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort']
    if not re.fullmatch(r'[0-9]{1,5}',port): raise SetupError('Не удалось определить порт поиска.')
    base='http://127.0.0.1:'+port
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect())
    for attempt in range(20):
        try:
            with opener.open(base+'/healthz',timeout=2) as response:
                if response.status==200: break
        except OSError: pass
        time.sleep(1)
    else: raise SetupError('Локальный поиск не запустился. Настройки OpenClaw ещё не изменены.')
    req=urllib.request.Request(base+'/search',urllib.parse.urlencode({'q':'SearXNG','format':'json'}).encode(),{'Content-Type':'application/x-www-form-urlencoded'})
    try:
        with opener.open(req,timeout=30) as response: raw=response.read(LIMIT+1)
        if len(raw)>LIMIT or not isinstance(json.loads(raw).get('results'),list): raise ValueError()
    except (OSError,ValueError): raise SetupError('Поиск не прошёл проверку JSON API. Настройки OpenClaw ещё не изменены.') from None
    return base


def preflight():
    if sys.platform != "linux" or os.geteuid() == 0:
        raise SetupError("Запустите в Linux под пользователем OpenClaw, без sudo и root.")
    expected = {"OPENCLAW_CONFIG_PATH": str(Path.home()/'.openclaw/openclaw.json'),
                "OPENCLAW_STATE_DIR": str(Path.home()/'.openclaw'), "OPENCLAW_HOME": str(Path.home())}
    for name, wanted in expected.items():
        if os.environ.get(name) and Path(os.environ[name]).resolve() != Path(wanted).resolve():
            raise SetupError("Для пилота нужен обычный профиль ~/.openclaw.")
    if os.environ.get('OPENCLAW_PROFILE'):
        raise SetupError("Именованные профили пока не поддерживаются.")
    for binary in ("openclaw", "systemctl", "openssl", "docker"):
        if not shutil.which(binary):
            raise SetupError("Не найдена команда " + binary + ". Сначала установите OpenClaw и его пользовательскую службу.")
    version = command(["openclaw", "--version"]).stdout
    endpoint=os.environ.get('DOCKER_HOST')
    if not endpoint:
        contexts=json.loads(command(['docker','context','inspect']).stdout)
        endpoint=contexts[0].get('Endpoints',{}).get('docker',{}).get('Host','')
    if not endpoint.startswith('unix://'):
        raise SetupError('Для локального поиска нужен Docker на этом же Linux-сервере.')
    command(['docker','info','--format','{{.ServerVersion}}'])
    if not re.search(r"\b2026\.8\.1\b", version):
        raise SetupError("Этот пилот рассчитан на OpenClaw 2026.8.1. Другую версию сначала нужно проверить.")
    root = Path.home() / ".openclaw"
    config = root / "openclaw.json"
    if root.is_symlink() or config.is_symlink() or not config.is_file() or config.stat().st_uid != os.geteuid():
        raise SetupError("Не найден собственный обычный профиль OpenClaw.")
    if config.stat().st_size > 1024 * 1024:
        raise SetupError("Конфигурация слишком большая.")
    try:
        original = json.loads(config.read_text())
    except ValueError:
        raise SetupError("Конфигурация должна быть JSON. Исходный файл не изменён.") from None
    if not isinstance(original, dict) or "$include" in original:
        raise SetupError("Профили с include пока не поддерживаются.")
    matrix = original.get("channels", {}).get("matrix", {})
    if matrix.get("accounts"):
        raise SetupError("Найдено несколько Matrix-аккаунтов. Этот пилот подключает один аккаунт.")
    if original.get("bindings"):
        raise SetupError("Найдена маршрутизация агентов. Перед пилотом её нужно проверить отдельно.")
    if set(original.get("agents", {}).get("entries", {})) - {"main"} or original.get("agents", {}).get("list"):
        raise SetupError("Этот пилот рассчитан на одного агента main.")
    unit = command(["systemctl", "--user", "show", SERVICE, "--property=LoadState", "--value"]).stdout.strip()
    if unit != "loaded":
        raise SetupError("Не найдена пользовательская служба OpenClaw.")
    # A profile override in the service would make CLI and Gateway write different accounts.
    service = command(["systemctl", "--user", "show", SERVICE, "--property=ExecStart", "--value"]).stdout
    env = command(["systemctl", "--user", "show", SERVICE, "--property=Environment", "--value"]).stdout
    env_values = dict(item.split('=',1) for item in shlex.split(env) if '=' in item)
    env_files = command(["systemctl", "--user", "show", SERVICE, "--property=EnvironmentFiles", "--value"]).stdout.strip()
    if '--profile' in service or env_values.get('OPENCLAW_PROFILE') or env_files:
        raise SetupError("Служба использует отдельный профиль или EnvironmentFile. Настройка требует проверки.")
    for name, wanted in {**expected, 'HOME':str(Path.home())}.items():
        if env_values.get(name) and Path(env_values[name]).resolve() != Path(wanted).resolve():
            raise SetupError("Служба использует другой профиль. Автоматическая настройка остановлена.")
    return root, config, original


def main():
    print("primoChat: OpenClaw + Kimi Code · " + VERSION)
    root, config, original = preflight()
    with installer_lock(root):
        install(root, config, original)


@contextmanager
def installer_lock(root):
    import fcntl
    fd=os.open(root/'.primochat-setup.lock',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
    try:
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise SetupError("Другой установщик уже работает. Дождитесь его завершения.") from None
        yield
    finally:
        os.close(fd)


def install(root, config, original):
    if not sys.stdin.isatty():
        raise SetupError("Запустите скачанный файл в интерактивном терминале.")
    print("Настраиваем профиль пользователя " + getpass.getuser() + ".")
    print("Ключ Kimi останется на этом компьютере. Сообщения нового чата будут обрабатываться Kimi.")
    print("Matrix-подключение этого профиля будет заменено; история чатов сохранится.")
    print('Локальный SearXNG установится автоматически. Поисковые запросы будут передаваться внешним поисковым системам.')
    if input("Продолжить? [да/нет]: ").strip().lower() not in ("да", "yes"):
        return
    owner = input("Ваш аккаунт primoChat (@имя:primochat.ru): ").strip()
    if not re.fullmatch(r"@[^\s:]+:primochat\.ru", owner):
        raise SetupError("Неверный аккаунт primoChat.")
    key = getpass.getpass("API-ключ из kimi.ai/code/console (ввод скрыт): ").strip()
    if not 16 <= len(key) <= 4096 or any(c.isspace() for c in key):
        raise SetupError("Неверный формат ключа.")
    print("Проверяем доступ к Kimi коротким запросом…")
    response = request_json(KIMI_BASE + "v1/messages", {
        "model": "kimi-for-coding", "max_tokens": 64,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": "Reply with OK."}],
    }, {"x-api-key": key, "anthropic-version": "2023-06-01", "User-Agent": "primoChat-OpenClaw-Setup/" + VERSION})
    if not any(item.get("type") == "text" and item.get("text", "").strip()
               for item in response.get("content", []) if isinstance(item, dict)):
        raise SetupError("Kimi не вернул текстовый ответ. Рабочая конфигурация не изменена.")
    print('Kimi отвечает. Запускаем локальный поиск SearXNG…')
    search_base=bundled_search(root)
    print("Поиск отвечает. Теперь создайте код подключения в Studio.")
    bootstrap = getpass.getpass("Код подключения Studio (ввод скрыт): ").strip()
    if not re.fullmatch(r"pcenr_[A-Za-z0-9_-]{43}", bootstrap):
        raise SetupError("Неверный формат кода подключения.")
    private = root / "primochat-setup"
    if private.is_symlink():
        raise SetupError("Небезопасный каталог настройки.")
    private.mkdir(mode=0o700, exist_ok=True)
    os.chmod(private, 0o700)
    enrollment = validate_enrollment(request_json(ENROLL, {"provider": "openclaw", "bootstrapCode": bootstrap}), owner)
    # Each attempt gets its own secrets file so a failed re-run cannot overwrite a live key.
    attempt = Path(tempfile.mkdtemp(prefix="attempt-", dir=private))
    os.chmod(attempt, 0o700)
    secret_file = attempt / "secrets.json"
    write_private(secret_file, {"kimiApiKey": key, "matrixAccessToken": enrollment["accessToken"]})
    write_private(attempt / "enrollment.json", enrollment)
    was_active = command(["systemctl", "--user", "is-active", SERVICE], check=False).returncode == 0
    before = None
    changed = False
    try:
        command(["systemctl", "--user", "stop", SERVICE])
        before = config.read_bytes()
        # Encrypt the rollback copy. The local wrapping key is separately permissioned.
        backup_key = attempt / "backup.key"
        write_private(backup_key, os.urandom(32).hex().encode())
        command(["openssl", "enc", "-aes-256-cbc", "-salt", "-pbkdf2", "-iter", "200000",
                 "-pass", "file:" + str(backup_key), "-in", str(config), "-out", str(attempt / "config.enc")])
        # Detect concurrent edits instead of overwriting them with the preflight snapshot.
        if json.loads(before) != original:
            raise SetupError("Конфигурация изменилась во время настройки. Запустите установщик заново.")
        plugins = command(["openclaw", "plugins", "list", "--json"]).stdout
        try:
            listed = json.loads(plugins)
        except ValueError:
            raise SetupError("Не удалось проверить установленные плагины.") from None
        entries = listed.get("plugins", []) if isinstance(listed, dict) else listed
        changed = True
        matrix = next((p for p in entries if p.get("id") == "matrix"), None)
        if matrix is None:
            print("Устанавливаем официальный Matrix-плагин версии 2026.8.1…")
            command(["openclaw", "plugins", "install", "npm:@openclaw/matrix@2026.8.1"], timeout=240)
        elif matrix.get("status") == "error":
            raise SetupError("Matrix-плагин сообщает об ошибке. Настройка остановлена.")
        if matrix is None or matrix.get("enabled") is not True:
            command(["openclaw", "plugins", "enable", "matrix"])
        search_plugin=next((p for p in entries if p.get('id')=='searxng'),None)
        if search_plugin is None:
            command(['openclaw','plugins','install','npm:@openclaw/searxng-plugin@2026.8.1'],timeout=240)
        elif search_plugin.get('status')=='error':
            raise SetupError('Плагин поиска сообщает об ошибке.')
        if search_plugin is None or search_plugin.get('enabled') is not True:
            command(['openclaw','plugins','enable','searxng'])
        replacement = configure(json.loads(config.read_bytes()), enrollment, secret_file, search_base)
        write_private(config, replacement)
        command(["openclaw", "config", "validate"])
        print("Проверяем Kimi через сам OpenClaw…")
        probe = json.loads(command(["openclaw", "models", "status", "--agent", "main", "--json",
            "--probe", "--probe-provider", "kimi", "--probe-timeout", "30000", "--probe-concurrency", "1", "--probe-max-tokens", "128"], timeout=120).stdout)
        if probe.get("resolvedDefault") != MODEL or not any(
                item.get("provider") == "kimi" and item.get("model") == MODEL
                and item.get("source") == "models.json" and item.get("status") == "ok"
                for item in probe.get("auth", {}).get("probes", {}).get("results", [])):
            raise SetupError("OpenClaw не подтвердил ответ с новым ключом Kimi. Прежние настройки восстановлены.")
        command(["systemctl", "--user", "start", SERVICE])
        print("Настройки применены. Откройте новый чат OpenClaw в primoChat и напишите обычное сообщение.")
        print("Подтвердите ответ в Studio. Наличие ключа и запуск службы сами по себе не подтверждают работу чата.")
        print('Также попросите помощника найти свежую информацию и привести ссылки: это проверит использование поиска в чате.')
    except BaseException:
        try:
            command(["systemctl", "--user", "stop", SERVICE], check=False)
        except SetupError:
            pass
        if changed and before is not None:
            write_private(config, before)
        if was_active:
            try:
                restarted=command(["systemctl", "--user", "start", SERVICE], check=False)
                if restarted.returncode:
                    print('Не удалось подтвердить запуск прежней службы. Проверьте её состояние.')
            except SetupError:
                print('Не удалось подтвердить запуск прежней службы. Проверьте её состояние.')
        print("Настройка не завершена. Прежняя конфигурация сохранена. Отключите неиспользованное подключение в Studio или перевыпустите код.")
        raise


if __name__ == "__main__":
    try:
        main()
    except SetupError as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("Настройка прервана.", file=sys.stderr)
        sys.exit(130)
    except Exception:
        print("Настройка остановлена. Диагностика скрыта, чтобы не раскрыть ключи.", file=sys.stderr)
        sys.exit(1)
