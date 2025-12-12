import json
import time
import os
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- 配置信息 ---
HOST = 'localhost'
PORT = 6800
RPC_SECRET = 'Brokye'
SAVE_FILE = 'aria2_links.txt'  # 保存链接的文件名


class Aria2MockHandler(BaseHTTPRequestHandler):
    def _set_headers(self):
        self.send_response(200)
        self.send_header('Content-type', 'application/json')
        # 允许跨域，防止浏览器插件报错
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', '*')
        self.end_headers()

    def do_OPTIONS(self):
        self._set_headers()

    def do_POST(self):
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)

        try:
            data = json.loads(post_data.decode('utf-8'))
            if isinstance(data, list):
                response_data = [self.process_request(req) for req in data]
            else:
                response_data = self.process_request(data)

            self._set_headers()
            self.wfile.write(json.dumps(response_data).encode('utf-8'))
        except json.JSONDecodeError:
            self.send_error(400, "Invalid JSON")
        except Exception as e:
            print(f"发生错误: {e}")
            self.send_error(500, str(e))

    def process_request(self, req):
        method = req.get('method')
        params = req.get('params', [])
        req_id = req.get('id')

        # 验证密钥: token:密钥 必须是第一个参数
        auth_token = f"token:{RPC_SECRET}"
        if not params or params[0] != auth_token:
            print(f"❌ [拒绝] 认证失败")
            return {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32602, "message": "Unauthorized"}}

        # --- 捕获逻辑 ---
        if method == 'aria2.addUri':
            try:
                uris = params[1]  # URL 列表

                # 控制台输出
                current_time = time.strftime('%H:%M:%S')
                print("-" * 50)
                print(f"🔥 [捕获成功] {current_time} | 数量: {len(uris)}")

                # 写入文件
                saved_count = 0
                with open(SAVE_FILE, 'a', encoding='utf-8') as f:
                    for uri in uris:
                        print(f"   👉 {uri}")
                        # 纯链接写入，一行一个，方便导入其他下载器
                        f.write(f"{uri}\n")
                        saved_count += 1

                print(f"💾 已保存 {saved_count} 个链接到: {SAVE_FILE}")
                print("-" * 50)

                # 返回假 GID 表示成功
                return {"jsonrpc": "2.0", "id": req_id, "result": "saved_to_txt_ok"}
            except Exception as e:
                print(f"⚠️  写入文件或解析错误: {e}")

        # 模拟 getVersion 防止插件报错
        elif method == 'aria2.getVersion':
            return {"jsonrpc": "2.0", "id": req_id, "result": {"enabledFeatures": [], "version": "1.36.0"}}

        return {"jsonrpc": "2.0", "id": req_id, "result": []}


def run():
    print(f"🚀 Aria2 链接捕获器已启动")
    print(f"📡 监听: http://{HOST}:{PORT}/jsonrpc")
    print(f"📂 保存位置: {os.path.abspath(SAVE_FILE)}")
    print("⏳ 等待浏览器发送链接... (Ctrl+C 停止)")

    server = HTTPServer((HOST, PORT), Aria2MockHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 服务已停止")
        server.server_close()


if __name__ == '__main__':
    run()
