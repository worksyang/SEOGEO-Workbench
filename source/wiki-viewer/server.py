#!/usr/bin/env python3
"""wiki-viewer 本地后端：Markdown 为唯一数据源。
启动后实时读写 output_md/ 目录下所有 .md 文件，无快照、无缓存、无备份。
文件树懒加载分层，支持全库文件名搜索。
用法：python3 server.py  然后访问 http://localhost:18923
"""
import json
import os
import posixpath
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.abspath(os.path.join(HERE, '..'))  # 整个 output_md
VIEWER_DIRNAME = os.path.basename(HERE)               # wiki-viewer 自身，树里隐藏
OCR_DB = os.path.join(HERE, 'ocr-db.json')
PORT = 18923


def load_ocr_db():
    if not os.path.exists(OCR_DB):
        return {}
    with open(OCR_DB, 'r', encoding='utf-8') as f:
        return json.load(f)


def url_core(url):
    """抽取图片 URL 的不变身份 ID。
    微信图 URL 形如 .../img_id/640?wx_fmt=png&from=appmsg#imgIndex=2
    其中 /640 是缩略尺寸（改成 /0 即原图）、?query 是格式参数、#anchor 是锚点，都会变；
    真正唯一不变的是 img_id 那段。这里统一去掉 #anchor、?query、末尾的 /尺寸，
    得到 core 作为 OCR 库的主键。同一张图无论 /640 还是 /0、带不带 from=appmsg，core 都一致。
    """
    u = url.split('#')[0]          # 去 #imgIndex 之类锚点
    u = u.split('?')[0]            # 去 ?wx_fmt=png&from=appmsg 之类 query
    u = re.sub(r'/\d+$', '', u)    # 去末尾 /640 /0 之类尺寸段
    return u


def safe_md_path(rel):
    """把前端传来的相对路径解析为 ROOT_DIR 内的绝对路径，拒绝穿越与非 md。"""
    if not rel or not rel.endswith('.md'):
        return None
    target = os.path.abspath(os.path.join(ROOT_DIR, rel))
    if target != ROOT_DIR and not target.startswith(ROOT_DIR + os.sep):
        return None
    return target


def safe_dir_path(rel):
    """把相对目录路径解析为 ROOT_DIR 内的绝对路径，拒绝穿越。空串=根。"""
    target = os.path.abspath(os.path.join(ROOT_DIR, rel)) if rel else ROOT_DIR
    if target != ROOT_DIR and not target.startswith(ROOT_DIR + os.sep):
        return None
    return target


def is_hidden(name):
    return name.startswith('.')


def list_dir(rel):
    """列出某一层目录内容（懒加载），返回 {dirs:[{name,path,count}], files:[{name,path}]}。
    count 是该子目录下 md 文件总数（递归），给前端显示数量用。"""
    base = safe_dir_path(rel)
    if not base or not os.path.isdir(base):
        return None
    dirs, files = [], []
    for name in sorted(os.listdir(base)):
        if is_hidden(name):
            continue
        full = os.path.join(base, name)
        relpath = (rel + '/' + name).lstrip('/') if rel else name
        if os.path.isdir(full):
            # 隐藏 viewer 自身
            if rel == '' and name == VIEWER_DIRNAME:
                continue
            dirs.append({'name': name, 'path': relpath, 'count': count_md(full)})
        elif name.endswith('.md'):
            files.append({'name': name, 'path': relpath})
    return {'dirs': dirs, 'files': files}


def count_md(abspath):
    """递归统计目录下 md 文件数。"""
    n = 0
    for root, ds, fs in os.walk(abspath):
        ds[:] = [d for d in ds if not is_hidden(d)]
        n += sum(1 for f in fs if f.endswith('.md'))
    return n


def search_md(query, limit=200):
    """全库按文件名/路径搜索 md（不区分大小写），返回相对路径列表。"""
    q = query.lower()
    hits = []
    for root, ds, fs in os.walk(ROOT_DIR):
        ds[:] = [d for d in ds if not is_hidden(d) and not (root == ROOT_DIR and d == VIEWER_DIRNAME)]
        for f in fs:
            if not f.endswith('.md'):
                continue
            rel = os.path.relpath(os.path.join(root, f), ROOT_DIR).replace(os.sep, '/')
            if q in rel.lower():
                hits.append(rel)
                if len(hits) >= limit:
                    return hits
    return hits


IMG_LINE_RE = re.compile(r'^!\[.*?\]\((.+)\)$')


def scan_image_in_file(content, core):
    """在单个 md 内容里找所有 core 匹配的图片行号（0-based）。"""
    lines = content.split('\n')
    hits = []
    for i, line in enumerate(lines):
        m = IMG_LINE_RE.match(line.strip())
        if m and url_core(m.group(1)) == core:
            hits.append(i)
    return hits


def compute_delete_ranges(content, core):
    """计算要删除的行范围列表 [(start, end_exclusive), ...]。
    每个匹配图片：图片行 + 后续 HTML 注释块 + --- 分隔线 + 多余空行。
    """
    lines = content.split('\n')
    img_indices = scan_image_in_file(content, core)
    ranges = []
    for img_idx in img_indices:
        start = img_idx
        end = img_idx + 1
        # 跳过空行
        while end < len(lines) and lines[end].strip() == '':
            end += 1
        # 检查 HTML 注释块
        if end < len(lines) and re.match(r'^<!--\s*(插图建议|OCR内容)', lines[end].strip()):
            while end < len(lines) and '-->' not in lines[end]:
                end += 1
            if end < len(lines):
                end += 1  # 包含 --> 所在行
        # 跳过空行
        while end < len(lines) and lines[end].strip() == '':
            end += 1
        # 检查 --- 分隔线
        if end < len(lines) and lines[end].strip() == '---':
            end += 1
        # 清理尾部多余空行
        while end < len(lines) and lines[end].strip() == '':
            end += 1
        ranges.append((start, end))
    return ranges


def remove_ranges(content, ranges):
    """从 content 中删除指定行范围，返回新内容。"""
    lines = content.split('\n')
    # 标记要删除的行
    kill = set()
    for start, end in ranges:
        for i in range(start, end):
            kill.add(i)
    # 重建，保留未删除行，过滤掉删除区域之间的多余空行
    result = []
    for i, line in enumerate(lines):
        if i not in kill:
            result.append(line)
    # 清理开头/结尾多余空行
    text = '\n'.join(result)
    return text


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, abspath, ctype):
        try:
            with open(abspath, 'rb') as f:
                body = f.read()
        except OSError:
            self.send_error(404, 'Not Found')
            return
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        qs = parse_qs(parsed.query)

        if route == '/api/ocr':
            url = unquote(qs.get('url', [''])[0]).strip()
            if not url:
                self._send_json({'error': 'missing url'}, 400)
                return
            db = load_ocr_db()
            # 用图片身份 ID(core) 匹配，忽略尺寸/query/锚点差异
            rec = db.get(url_core(url))
            if rec:
                self._send_json({'ok': True, 'ocr': rec['ocr'], 'source': rec.get('source', '')})
            else:
                self._send_json({'ok': False, 'ocr': ''})
            return

        if route == '/api/list':
            # 懒加载：返回某一层目录内容；path 为空=根
            rel = unquote(qs.get('path', [''])[0])
            data = list_dir(rel)
            if data is None:
                self._send_json({'error': 'invalid dir'}, 404)
                return
            self._send_json(data)
            return

        if route == '/api/search':
            q = unquote(qs.get('q', [''])[0]).strip()
            if not q:
                self._send_json({'files': []})
                return
            self._send_json({'files': search_md(q)})
            return

        if route == '/api/file':
            rel = unquote(qs.get('path', [''])[0])
            target = safe_md_path(rel)
            if not target or not os.path.isfile(target):
                self._send_json({'error': 'not found'}, 404)
                return
            with open(target, 'r', encoding='utf-8') as f:
                content = f.read()
            self._send_json({'path': rel, 'content': content})
            return

        if route == '/api/scan-image':
            core = unquote(qs.get('core', [''])[0]).strip()
            if not core:
                self._send_json({'error': 'missing core'}, 400)
                return
            results = []
            total_hits = 0
            for root, ds, fs in os.walk(ROOT_DIR):
                ds[:] = [d for d in ds if not is_hidden(d) and not (root == ROOT_DIR and d == VIEWER_DIRNAME)]
                for f in fs:
                    if not f.endswith('.md'):
                        continue
                    abspath = os.path.join(root, f)
                    try:
                        with open(abspath, 'r', encoding='utf-8') as fh:
                            content = fh.read()
                    except (OSError, UnicodeDecodeError):
                        continue
                    hits = scan_image_in_file(content, core)
                    if hits:
                        relpath = os.path.relpath(abspath, ROOT_DIR).replace(os.sep, '/')
                        results.append({'path': relpath, 'count': len(hits)})
                        total_hits += len(hits)
            self._send_json({'ok': True, 'total': total_hits, 'files': results})
            return

        if route == '/api/image-index':
            # 一次性扫描全库，构建 core -> {total, files} 反向索引
            index = {}
            for root, ds, fs in os.walk(ROOT_DIR):
                ds[:] = [d for d in ds if not is_hidden(d) and not (root == ROOT_DIR and d == VIEWER_DIRNAME)]
                for f in fs:
                    if not f.endswith('.md'):
                        continue
                    abspath = os.path.join(root, f)
                    try:
                        with open(abspath, 'r', encoding='utf-8') as fh:
                            content = fh.read()
                    except (OSError, UnicodeDecodeError):
                        continue
                    lines = content.split('\n')
                    file_counts = {}  # core -> count in this file
                    for line in lines:
                        m = IMG_LINE_RE.match(line.strip())
                        if m:
                            c = url_core(m.group(1))
                            file_counts[c] = file_counts.get(c, 0) + 1
                    if not file_counts:
                        continue
                    relpath = os.path.relpath(abspath, ROOT_DIR).replace(os.sep, '/')
                    for c, cnt in file_counts.items():
                        if c not in index:
                            index[c] = {'total': 0, 'files': []}
                        index[c]['total'] += cnt
                        index[c]['files'].append({'path': relpath, 'count': cnt})
            self._send_json({'ok': True, 'index': index})
            return

        # 静态文件服务（仅限 wiki-viewer 目录内）
        self._serve_static(route)

    def _serve_static(self, route):
        if route == '/' or route == '':
            route = '/wiki.html'
        rel = posixpath.normpath(unquote(route)).lstrip('/')
        abspath = os.path.abspath(os.path.join(HERE, rel))
        if abspath != HERE and not abspath.startswith(HERE + os.sep):
            self.send_error(403, 'Forbidden')
            return
        ext = os.path.splitext(abspath)[1].lower()
        ctype = {
            '.html': 'text/html; charset=utf-8',
            '.js': 'application/javascript; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.json': 'application/json; charset=utf-8',
            '.png': 'image/png', '.jpg': 'image/jpeg', '.svg': 'image/svg+xml',
        }.get(ext, 'application/octet-stream')
        self._send_file(abspath, ctype)

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == '/api/bulk-delete-image':
            length = int(self.headers.get('Content-Length', 0))
            try:
                data = json.loads(self.rfile.read(length).decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                self._send_json({'error': 'bad json'}, 400)
                return
            core = data.get('core', '').strip()
            if not core:
                self._send_json({'error': 'missing core'}, 400)
                return
            deleted_files = 0
            deleted_images = 0
            for root, ds, fs in os.walk(ROOT_DIR):
                ds[:] = [d for d in ds if not is_hidden(d) and not (root == ROOT_DIR and d == VIEWER_DIRNAME)]
                for f in fs:
                    if not f.endswith('.md'):
                        continue
                    abspath = os.path.join(root, f)
                    try:
                        with open(abspath, 'r', encoding='utf-8') as fh:
                            content = fh.read()
                    except (OSError, UnicodeDecodeError):
                        continue
                    ranges = compute_delete_ranges(content, core)
                    if not ranges:
                        continue
                    new_content = remove_ranges(content, ranges)
                    deleted_images += len(ranges)
                    try:
                        with open(abspath, 'w', encoding='utf-8') as fh:
                            fh.write(new_content)
                        deleted_files += 1
                    except OSError:
                        pass
            # 同时清理 ocr-db.json
            db = load_ocr_db()
            if core in db:
                del db[core]
                try:
                    with open(OCR_DB, 'w', encoding='utf-8') as fh:
                        json.dump(db, fh, ensure_ascii=False, indent=2)
                except OSError:
                    pass
            self._send_json({'ok': True, 'deleted_files': deleted_files, 'deleted_images': deleted_images})
            return

        if parsed.path != '/api/save':
            self._send_json({'error': 'unknown endpoint'}, 404)
            return
        length = int(self.headers.get('Content-Length', 0))
        try:
            data = json.loads(self.rfile.read(length).decode('utf-8'))
        except (ValueError, UnicodeDecodeError):
            self._send_json({'error': 'bad json'}, 400)
            return
        rel = data.get('path', '')
        content = data.get('content', '')
        target = safe_md_path(rel)
        if not target:
            self._send_json({'error': 'invalid path'}, 400)
            return
        if not isinstance(content, str):
            self._send_json({'error': 'content must be string'}, 400)
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, 'w', encoding='utf-8') as f:
            f.write(content)
        self._send_json({'ok': True, 'path': rel})

    def log_message(self, fmt, *args):
        pass  # 静默，避免刷屏


def get_lan_ip():
    """获取本机内网 IP（用于打印访问地址）。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
    except OSError:
        ip = '127.0.0.1'
    finally:
        s.close()
    return ip


def main():
    if not os.path.isdir(ROOT_DIR):
        raise SystemExit('找不到数据目录: ' + ROOT_DIR)
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    lan = get_lan_ip()
    print('知识库浏览器后端已启动（内网可访问）')
    print('  数据源:   ' + ROOT_DIR + ' （整库，懒加载）')
    print('  本机访问: http://localhost:' + str(PORT))
    print('  内网访问: http://' + lan + ':' + str(PORT))
    print('  注意: 同一局域网内任何设备都可读写知识库，仅在可信网络下使用')
    print('  Ctrl+C 停止')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n已停止')
        server.shutdown()


if __name__ == '__main__':
    main()
