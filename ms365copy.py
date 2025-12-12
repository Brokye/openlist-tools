import requests
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

# --- 配置信息 ---
TENANT_ID = ""
CLIENT_ID = ""
CLIENT_SECRET = ""
SOURCE_USER_EMAIL = ""
TARGET_USER_EMAIL = ""

# 路径配置
SOURCE_FOLDER_PATH = "/15/Original/3001-3214"
TARGET_FOLDER_PATH = "/15/Original/3001-3214"

# 并发配置
# 建议：如果经常丢文件，适当降低并发数反而能提高成功率
MAX_WORKERS = 15
MAX_RETRIES_PER_FOLDER = 4  # 每个文件夹校验补漏的尝试次数

GRAPH_BASE_URL = "https://graph.microsoft.com/v1.0"

# 全局统计与锁
stats = {
    "copied": 0,
    "skipped": 0,
    "failed": 0,
    "folders": 0,
    "retried": 0
}
print_lock = threading.Lock()


class TokenManager:
    """自动管理 Token 的类，过期自动刷新"""

    def __init__(self):
        self.token = None
        self.expires_at = 0

    def get_token(self):
        if not self.token or time.time() > self.expires_at - 300:
            with print_lock:
                # print("   [系统] 正在刷新 Token...")
                pass
            self._refresh_token()
        return self.token

    def _refresh_token(self):
        url = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        data = {
            'client_id': CLIENT_ID, 'scope': 'https://graph.microsoft.com/.default',
            'client_secret': CLIENT_SECRET, 'grant_type': 'client_credentials'
        }
        try:
            resp = requests.post(url, headers=headers, data=data)
            resp.raise_for_status()
            js = resp.json()
            self.token = js['access_token']
            self.expires_at = time.time() + int(js.get('expires_in', 3600))
        except Exception as e:
            print(f"[FATAL] Token 刷新失败: {e}")
            raise e

    def get_headers(self, content_type=None):
        headers = {'Authorization': f'Bearer {self.get_token()}'}
        if content_type: headers['Content-Type'] = content_type
        return headers


token_manager = TokenManager()


# --- 核心工具函数 ---

def safe_request(method, url, **kwargs):
    """带重试机制的请求封装"""
    retries = 4
    delay = 1

    if 'headers' not in kwargs:
        kwargs['headers'] = token_manager.get_headers()

    for i in range(retries):
        try:
            if method == 'GET':
                resp = requests.get(url, **kwargs)
            elif method == 'POST':
                resp = requests.post(url, **kwargs)

            if resp.status_code == 429:
                retry_after = int(resp.headers.get('Retry-After', delay * 2))
                with print_lock:
                    print(f"   [流控] API 限制，暂停 {retry_after} 秒...")
                time.sleep(retry_after)
                continue

            if resp.status_code in [500, 502, 503, 504]:
                time.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 401:
                token_manager._refresh_token()
                kwargs['headers'] = token_manager.get_headers()
                continue

            return resp
        except requests.exceptions.RequestException as e:
            if i == retries - 1: raise e
            time.sleep(delay)
            delay *= 2
    return None


def list_children_map(drive_id, item_id):
    """
    列出目录下所有项目，增加分页鲁棒性
    """
    url = f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{item_id}/children?$top=999"
    items_map = {}

    while url:
        resp = safe_request('GET', url)
        if not resp or resp.status_code != 200:
            # 如果列表获取失败，抛出异常以便外层重试
            raise Exception(f"Failed to list children: {resp.status_code if resp else 'No Resp'}")

        data = resp.json()
        for item in data.get('value', []):
            items_map[item['name']] = item
        url = data.get('@odata.nextLink')

    return items_map


def get_or_create_folder(drive_id, parent_id, folder_name):
    """
    获取或创建文件夹 (Debug 增强版)
    """
    url = f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{parent_id}/children"
    payload = {
        "name": folder_name, "folder": {}, "@microsoft.graph.conflictBehavior": "fail"
    }

    # 打印正在尝试的操作，方便定位
    # print(f"   [Debug] 尝试在父目录 {parent_id} 下创建/查找: {folder_name}")

    resp = safe_request('POST', url, json=payload, headers=token_manager.get_headers('application/json'))

    if resp is None:
        print(f"   [Error] 请求无响应 (None)，可能是网络问题或重试耗尽。")
        return None, False

    # 1. 创建成功
    if resp.status_code == 201:
        with print_lock:
            print(f"   [新建目录] {folder_name}")
        return resp.json()['id'], True

    # 2. 已存在 (409 Conflict) -> 转为查询
    elif resp.status_code == 409:
        # 已存在，查询 ID
        filter_url = f"{GRAPH_BASE_URL}/drives/{drive_id}/items/{parent_id}/children?$filter=name eq '{folder_name}'"
        search_resp = safe_request('GET', filter_url)

        if search_resp and search_resp.status_code == 200:
            val = search_resp.json().get('value')
            if val:
                return val[0]['id'], False
            else:
                print(f"   [Error] 409 冲突但无法查询到文件夹 ID: {folder_name}")
        else:
            print(f"   [Error] 查询已存在文件夹失败: {search_resp.status_code if search_resp else 'None'}")

    # 3. 其他错误 (权限、路径等) -> 打印详细报错！
    else:
        with print_lock:
            print(f"   [API 错误] 创建文件夹失败 '{folder_name}'")
            print(f"   Status Code: {resp.status_code}")
            print(f"   Response: {resp.text}")  # 这里会显示微软的具体报错信息

    return None, False


def copy_single_file_task(source_drive, file_id, target_drive, target_parent_id, file_name):
    """单个文件复制任务"""
    url = f"{GRAPH_BASE_URL}/drives/{source_drive}/items/{file_id}/copy"
    payload = {
        "parentReference": {"driveId": target_drive, "id": target_parent_id},
        "name": file_name
    }

    try:
        resp = safe_request('POST', url, json=payload, headers=token_manager.get_headers('application/json'))
        if resp and resp.status_code == 202:
            return True, file_name
        else:
            code = resp.status_code if resp else "None"
            return False, f"{file_name} (Code: {code})"
    except Exception as e:
        return False, f"{file_name} (Err: {e})"


def process_folder_robust(source_drive, source_id, target_drive, target_id, current_path):
    """
    [核心改进] 健壮的文件夹处理逻辑：校验 -> 复制 -> 再校验 -> 补漏
    """
    global stats

    # 1. 识别需要递归的子文件夹 (只需做一次，因为文件夹结构相对固定，主要是文件容易丢)
    try:
        source_items_map = list_children_map(source_drive, source_id)
    except Exception as e:
        print(f"   [!] 无法读取源目录 {current_path}: {e}")
        return

    folders_to_recurse = [item for item in source_items_map.values() if 'folder' in item]

    # 2. 循环处理文件，确保所有文件都已传输
    # 我们使用一个循环，如果在一次复制后发现还有缺失文件，就再次尝试

    for attempt in range(MAX_RETRIES_PER_FOLDER):
        # 每次循环都重新获取目标目录的文件列表，确保状态最新
        try:
            target_items_map = list_children_map(target_drive, target_id)
        except Exception as e:
            print(f"   [!] 读取目标目录失败，重试中... {e}")
            time.sleep(2)
            continue

        files_to_copy = []

        # 对比源和目标，找出缺失文件
        for name, item in source_items_map.items():
            if 'file' in item:
                if name not in target_items_map:
                    files_to_copy.append(item)

        # 如果没有缺失文件，说明本文件夹同步完成，跳出循环
        if not files_to_copy:
            if attempt == 0:
                pass  # 一次性成功
            else:
                with print_lock:
                    print(f"   [√ 补漏成功] {current_path} (尝试 {attempt + 1} 次)")
            break

        # 打印状态
        with print_lock:
            if attempt == 0:
                print(f"\n处理目录: {current_path}")
                print(f"   - 待复制文件: {len(files_to_copy)} / 总文件: {len(source_items_map)}")
            else:
                print(f"   [重试 {attempt}] {current_path} 发现 {len(files_to_copy)} 个文件缺失，正在补漏...")
                stats['retried'] += len(files_to_copy)

        # 提交并发任务
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = []
            for file_item in files_to_copy:
                futures.append(
                    executor.submit(
                        copy_single_file_task,
                        source_drive,
                        file_item['id'],
                        target_drive,
                        target_id,
                        file_item['name']
                    )
                )

            # 处理结果
            success_count = 0
            for future in as_completed(futures):
                is_success, msg = future.result()
                if is_success:
                    success_count += 1
                    with print_lock:
                        stats['copied'] += 1
                        print(f"   [C] {msg}", end="\r")
                else:
                    with print_lock:
                        stats['failed'] += 1  # 暂时计入失败，下一轮循环会重试
                        # print(f"   [X] {msg}") # 保持控制台清爽，不打印单个失败

        # 等待服务器处理（Graph API Copy 是异步的，给它一点时间落地）
        # 如果文件很多，API 延迟会变大
        wait_time = 3 + (attempt * 2)
        time.sleep(wait_time)

    else:
        # 如果循环结束（达到最大重试次数）仍有文件缺失
        with print_lock:
            print(f"   [!!! 警告] 目录 {current_path} 在 {MAX_RETRIES_PER_FOLDER} 次尝试后仍有文件未完成同步。")

    # 3. 递归处理子文件夹
    for folder_item in folders_to_recurse:
        folder_name = folder_item['name']

        # 获取或创建目标文件夹
        sub_target_id, is_new = get_or_create_folder(target_drive, target_id, folder_name)
        if is_new:
            stats['folders'] += 1

        if sub_target_id:
            process_folder_robust(
                source_drive,
                folder_item['id'],
                target_drive,
                sub_target_id,
                current_path + "/" + folder_name
            )


# --- 入口函数保持大致不变 ---

def get_drive_id(user_email):
    url = f"{GRAPH_BASE_URL}/users/{user_email}/drive"
    resp = safe_request('GET', url)
    resp.raise_for_status()
    return resp.json()['id']


def get_item_id_by_path(drive_id, path):
    if not path or path.strip() == "":
        url = f"{GRAPH_BASE_URL}/drives/{drive_id}/root"
    else:
        encoded_path = urllib.parse.quote(path)
        url = f"{GRAPH_BASE_URL}/drives/{drive_id}/root:/{encoded_path}"

    resp = safe_request('GET', url)
    if resp and resp.status_code == 404: return None
    if resp: resp.raise_for_status()
    return resp.json()['id']


def create_target_path_tree(drive_id, root_id, path_str):
    if not path_str or path_str.strip() == "": return root_id
    parts = [p for p in path_str.replace('\\', '/').split('/') if p]
    current_id = root_id
    for part in parts:
        current_id, _ = get_or_create_folder(drive_id, current_id, part)
        if not current_id: raise Exception(f"创建路径失败: {part}")
    return current_id


def main():
    start_time = time.time()
    try:
        print("=== 微软 Graph API 文件同步 (健壮版 v3.0) ===")
        print(f"策略: Verify-Copy-Verify | 线程数: {MAX_WORKERS} | 重试轮次: {MAX_RETRIES_PER_FOLDER}")

        token_manager.get_token()  # 预热 Token

        s_drive = get_drive_id(SOURCE_USER_EMAIL)
        t_drive = get_drive_id(TARGET_USER_EMAIL)

        print(f"源 Drive ID: {s_drive}")
        print(f"目标 Drive ID: {t_drive}")

        s_root = get_item_id_by_path(s_drive, SOURCE_FOLDER_PATH)
        if not s_root:
            print(f"错误: 源路径不存在 {SOURCE_FOLDER_PATH}")
            return

        # 获取目标根
        t_drive_root_resp = safe_request('GET', f"{GRAPH_BASE_URL}/drives/{t_drive}/root")
        t_root_id = t_drive_root_resp.json()['id']
        t_start = create_target_path_tree(t_drive, t_root_id, TARGET_FOLDER_PATH)

        # 开始递归处理
        process_folder_robust(s_drive, s_root, t_drive, t_start, SOURCE_FOLDER_PATH)

        duration = time.time() - start_time
        print(f"\n\n=== 任务全部完成 ===")
        print(f"耗时: {duration:.2f} 秒")
        print(f"发起复制请求: {stats['copied']}")
        print(f"新建文件夹:   {stats['folders']}")
        print(f"触发补漏次数: {stats['retried']}")

    except KeyboardInterrupt:
        print("\n[用户终止] 正在停止...")
    except Exception as e:
        print(f"\n[程序崩溃] {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
