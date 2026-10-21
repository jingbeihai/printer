"""正式桌面端：内置页面，同源转发到用户配置的云端服务。"""
from http.cookies import SimpleCookie
from pathlib import Path
import threading
import time
import tempfile
from urllib.parse import urlsplit

import requests
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .webcommon import render


def create_desktop_app(cloud_url: str, station=None, updater=None) -> FastAPI:
    cloud_url = cloud_url.rstrip('/')
    parsed = urlsplit(cloud_url)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        raise ValueError('云端地址无效，请检查客户端设置')
    app = FastAPI(docs_url=None, redoc_url=None)

    @app.middleware('http')
    async def local_only(request: Request, call_next):
        if request.url.hostname not in ('127.0.0.1', 'localhost'):
            return Response(status_code=403)
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return Response(status_code=403)
        if (station is not None and getattr(station, 'update_blocked', False) is True and request.method not in ('GET', 'HEAD')
                and request.url.path != '/desktop/update/install'):
            return JSONResponse({'ok': False, 'error': '客户端正在更新，请稍后'}, status_code=503)
        response = await call_next(request)
        response.headers['Cache-Control'] = 'no-store'
        return response

    def send(method, path, body, headers):
        # 每次请求独立持有会话，避免用户间共享 Cookie。直连可绕开失效的环境代理。
        with requests.Session() as session:
            session.trust_env = False
            return session.request(method, cloud_url + path, data=body, headers=headers,
                                   timeout=(10, 90), allow_redirects=False)

    async def forward(request: Request, path: str):
        headers = {name: request.headers[name] for name in ('content-type', 'cookie')
                   if name in request.headers}
        return await run_in_threadpool(send, request.method, path, await request.body(), headers)

    async def cloud_identity(request: Request):
        try:
            cookie = request.headers.get('cookie', '')
            upstream = await run_in_threadpool(send, 'GET', '/api/v1/bootstrap', None,
                                               {'cookie': cookie} if cookie else {})
        except requests.RequestException:
            return None, JSONResponse({'ok': False, 'error': '无法连接云端'}, status_code=502)
        if upstream.status_code != 200:
            return None, JSONResponse({'ok': False, 'error': '请重新登录'}, status_code=401)
        if station is not None and cookie:
            station.bind_session(cookie)
        return upstream.json(), None

    def local_failure(error):
        return JSONResponse({'ok': False, 'error': str(error)},
                            status_code=400 if isinstance(error, ValueError) else 502)

    def transfer_cookie(upstream, response):
        for raw in upstream.raw.headers.getlist('Set-Cookie'):
            cookie = SimpleCookie()
            cookie.load(raw)
            if 'pc_session' not in cookie:
                continue
            item = cookie['pc_session']
            response.set_cookie('pc_session', item.value, httponly=True, samesite='lax',
                                max_age=int(item['max-age']) if item['max-age'] else None,
                                expires=item['expires'] or None)
        return response

    @app.get('/')
    def index():
        return RedirectResponse('/login', 303)

    @app.get('/login')
    def login_page():
        return render('login.html', error='')

    @app.post('/login')
    async def login(request: Request):
        try:
            upstream = await forward(request, '/login')
        except requests.RequestException:
            return render('login.html', error='无法连接云端，请检查网络后重试')
        if upstream.status_code in (302, 303) and '/desk' in upstream.headers.get('location', ''):
            return transfer_cookie(upstream, RedirectResponse('/desk/', 303))
        return render('login.html', error='用户名或密码错误' if upstream.status_code == 200
                      else '云端暂时无法登录，请稍后重试')

    @app.get('/logout')
    def logout():
        if station is not None:
            station.clear_session()
        response = RedirectResponse('/login', 303)
        response.delete_cookie('pc_session')
        return response

    @app.api_route('/register', methods=['GET', 'POST'])
    async def register(request: Request):
        if request.method == 'GET':
            return render('register.html', error='', invite=request.query_params.get('invite', ''))
        try:
            upstream = await forward(request, '/register')
        except requests.RequestException:
            return render('register.html', error='无法连接云端，请检查网络后重试', invite='')
        if upstream.status_code in (302, 303):
            return transfer_cookie(upstream, RedirectResponse('/desk/', 303))
        return Response(upstream.content, status_code=upstream.status_code, media_type='text/html')

    @app.get('/desktop/printers/available')
    async def available_printers(request: Request):
        identity, error = await cloud_identity(request)
        if error:
            return error
        if station is None:
            return JSONResponse({'ok': False, 'error': '此客户端没有打印站服务'}, status_code=503)
        try:
            return {'ok': True, 'printers': await run_in_threadpool(station.available_printers)}
        except Exception as exc:
            return local_failure(exc)

    @app.post('/desktop/printers')
    async def add_printer(request: Request):
        identity, error = await cloud_identity(request)
        if error:
            return error
        if station is None:
            return JSONResponse({'ok': False, 'error': '此客户端没有打印站服务'}, status_code=503)
        body = await request.json()
        try:
            pid = await run_in_threadpool(station.add_printer, str(body.get('name', '')),
                                          str(body.get('queue', '')), str(body.get('ip', '')))
        except (ValueError, RuntimeError, OSError) as exc:
            return local_failure(exc)
        return {'ok': True, 'id': pid}

    @app.delete('/desktop/printers/{pid}')
    async def delete_printer(request: Request, pid: int):
        identity, error = await cloud_identity(request)
        if error:
            return error
        if station is None:
            return JSONResponse({'ok': False, 'error': '此客户端没有打印站服务'}, status_code=503)
        try:
            await run_in_threadpool(station.delete_printer, pid)
        except ValueError as exc:
            return local_failure(exc)
        return {'ok': True}

    @app.post('/desktop/tasks/{tid}/print')
    async def print_task(request: Request, tid: int):
        identity, error = await cloud_identity(request)
        if error:
            return error
        task = next((item for item in identity['tasks'] if item['id'] == tid), None)
        if not task:
            return JSONResponse({'ok': False, 'error': '任务不存在'}, status_code=404)
        if task['status'] != 'QUEUED':
            return JSONResponse({'ok': False, 'error': '该任务已经开始或结束'}, status_code=400)
        if station is None:
            return JSONResponse({'ok': False, 'error': '此客户端没有打印站服务'}, status_code=503)
        body = await request.json()
        try:
            ids = [int(value) for value in body.get('printer_ids', [])]
            await run_in_threadpool(station.request_print, task, ids)
        except (ValueError, RuntimeError, TypeError, requests.RequestException, OSError) as exc:
            return local_failure(exc)
        return {'ok': True}

    @app.get('/desktop/update')
    async def update_status(request: Request):
        return {'ok': True, **updater.status()} if updater else {'ok': True, 'enabled': False}

    @app.get('/downloads/client-latest.zip')
    async def client_download(request: Request):
        """Proxy the public cloud download so the desktop link stays same-origin."""
        try:
            upstream = await run_in_threadpool(send, 'GET',
                                               '/downloads/client-latest.zip', None, {})
        except requests.RequestException:
            return JSONResponse({'ok': False, 'error': '无法连接云端下载服务'}, status_code=502)
        if upstream.status_code != 200:
            return Response(upstream.content, status_code=upstream.status_code,
                            media_type=upstream.headers.get('content-type', 'text/plain'))
        response = Response(upstream.content, media_type='application/zip')
        if upstream.headers.get('content-disposition'):
            response.headers['Content-Disposition'] = upstream.headers['content-disposition']
        return response

    def update_access(request: Request):
        if not request.cookies.get('pc_session'):
            return JSONResponse({'ok': False, 'error': '请先登录客户端'}, status_code=401)
        return None

    @app.post('/desktop/update/check')
    async def update_check(request: Request):
        error = update_access(request)
        if error:
            return error
        try:
            return {'ok': True, **(await run_in_threadpool(updater.check))}
        except Exception as exc:
            return local_failure(exc)

    @app.post('/desktop/update/download')
    async def update_download(request: Request):
        error = update_access(request)
        if error:
            return error
        try:
            return await run_in_threadpool(updater.download)
        except Exception as exc:
            return local_failure(exc)

    @app.post('/desktop/update/import')
    async def update_import(request: Request):
        error = update_access(request)
        if error:
            return error
        if updater is None:
            return JSONResponse({'ok': False, 'error': '更新服务不可用'}, status_code=503)
        form = await request.form()
        manifest_file, archive_file = form.get('manifest'), form.get('archive')
        if not hasattr(manifest_file, 'read') or not hasattr(archive_file, 'read'):
            return JSONResponse({'ok': False, 'error': '请选择清单 JSON 与升级 ZIP'}, status_code=400)
        raw = await manifest_file.read(32769)
        temporary = None
        try:
            folder = updater.data_dir / 'updates'
            folder.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=folder, prefix='import-', suffix='.part', delete=False) as target:
                temporary = Path(target.name)
                total = 0
                while chunk := await archive_file.read(1024 * 1024):
                    total += len(chunk)
                    if total > 600 * 1024 * 1024:
                        raise ValueError('离线包超过大小限制')
                    target.write(chunk)
            return {'ok': True, **(await run_in_threadpool(updater.import_offline, raw, temporary))}
        except Exception as exc:
            return local_failure(exc)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)

    @app.post('/desktop/update/install')
    async def update_install(request: Request):
        error = update_access(request)
        if error:
            return error
        prepared = False
        try:
            manifest, archive = await run_in_threadpool(updater.ready_install)
            prepared = True
            await run_in_threadpool(station.prepare_update)
            await run_in_threadpool(updater.install, manifest, archive)
            def close_window():
                time.sleep(.5)
                import webview
                if webview.windows:
                    webview.windows[0].destroy()
            threading.Thread(target=close_window, daemon=True).start()
            return {'ok': True, 'message': '客户端即将退出并安装新版'}
        except Exception as exc:
            if prepared:
                await run_in_threadpool(station.cancel_update)
            return local_failure(exc)

    @app.post('/desktop/tasks/{tid}/pickup')
    async def pickup_task(request: Request, tid: int):
        identity, error = await cloud_identity(request)
        if error:
            return error
        if station is None:
            return JSONResponse({'ok': False, 'error': '此客户端没有打印站服务'}, status_code=503)
        try:
            await run_in_threadpool(station.confirm_pickup, tid)
        except ValueError as exc:
            return local_failure(exc)
        return {'ok': True}

    @app.get('/desktop/fleet')
    async def fleet(request: Request):
        identity, error = await cloud_identity(request)
        if error:
            return error
        if station is None:
            return JSONResponse({'ok': False, 'error': '此客户端没有打印站服务'}, status_code=503)
        return {'ok': True, 'tasks': await run_in_threadpool(station.fleet_tasks)}

    @app.get('/desktop/logs')
    async def runtime_logs(request: Request):
        identity, error = await cloud_identity(request)
        if error:
            return error
        if station is None:
            return JSONResponse({'ok': False, 'error': '此客户端没有打印站服务'}, status_code=503)
        try:
            task_id = request.query_params.get('task_id')
            group_id = request.query_params.get('group_id')
            rows = await run_in_threadpool(
                station.runtime_logs, identity['user']['username'], identity['user']['role'],
                int(task_id) if task_id else None, int(group_id) if group_id else None, 500)
        except ValueError:
            return JSONResponse({'ok': False, 'error': '筛选条件无效'}, status_code=400)
        return {'ok': True, 'logs': rows}

    @app.post('/desktop/tasks/{tid}/control')
    async def control_task(request: Request, tid: int):
        identity, error = await cloud_identity(request)
        if error:
            return error
        if station is None:
            return JSONResponse({'ok': False, 'error': '此客户端没有打印站服务'}, status_code=503)
        body = await request.json()
        try:
            message = await run_in_threadpool(station.control_task, tid, body.get('action'),
                                              body.get('printer_id'), identity['user']['username'])
        except (ValueError, RuntimeError, OSError) as exc:
            return local_failure(exc)
        return {'ok': True, 'message': message or ''}

    @app.post('/desktop/tasks/{tid}/classes/{gid}/resolve')
    async def resolve_class_group(request: Request, tid: int, gid: int):
        identity, error = await cloud_identity(request)
        if error:
            return error
        if station is None:
            return JSONResponse({'ok': False, 'error': '此客户端没有打印站服务'}, status_code=503)
        body = await request.json()
        try:
            message = await run_in_threadpool(station.resolve_class_group, tid, gid,
                                              body.get('action'), bool(body.get('confirm_stopped')),
                                              bool(body.get('confirm_unmatched')))
        except (ValueError, RuntimeError, OSError) as exc:
            return local_failure(exc)
        return {'ok': True, 'message': message or ''}

    @app.api_route('/api/v1/{path:path}', methods=['GET', 'POST', 'PUT', 'DELETE'])
    async def api(request: Request, path: str):
        suffix = '?' + request.url.query if request.url.query else ''
        try:
            upstream = await forward(request, '/api/v1/' + path + suffix)
        except requests.RequestException:
            return JSONResponse({'ok': False, 'error': '连接云端失败，请检查网络'}, status_code=502)
        if path == 'bootstrap' and upstream.status_code == 200:
            data = upstream.json()
            data['client_kind'] = 'desktop'
            if station and request.headers.get('cookie'):
                station.bind_session(request.headers['cookie'])
            data['station_available'] = bool(station and station.enabled)
            data['station_ready'] = bool(station and station.ready)
            data['station_sync_error'] = station.syncer.last_error if station and station.syncer else ''
            data['printers'] = station.snapshot() if station and station.enabled else []
            data['station_pending_print_ids'] = station.pending_print_ids() if station and station.enabled else []
            data['station_pickup_ids'] = station.pickup_ids() if station and station.enabled else []
            return JSONResponse(data)
        return Response(upstream.content, status_code=upstream.status_code,
                        headers={'Content-Type': upstream.headers.get('content-type', 'application/json')})

    assets = Path(__file__).parent / 'static' / 'desk'
    app.mount('/_next', StaticFiles(directory=str(assets / '_next')), name='desktop-next')
    app.mount('/desk', StaticFiles(directory=str(assets), html=True), name='desktop-ui')
    return app
