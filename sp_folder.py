import requests
import json
import time
import math
import re  # 引入正则模块用于提取数字

# ================= 配置区域 =================
TENANT_ID = ''
CLIENT_ID = ''
CLIENT_SECRET = ''
SITE_URL = ''
TARGET_FOLDER_PATH = ''


# ===========================================

class SharePointCustomSortBatch:
    def __init__(self):
        self.token = None
        self.token_expires_at = 0
        self.base_url = 'https://graph.microsoft.com/v1.0'
        self.get_valid_token()

    def get_valid_token(self):
        """自动获取或刷新 Token"""
        if not self.token or time.time() > self.token_expires_at:
            url = f'https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token'
            data = {
                'grant_type': 'client_credentials',
                'client_id': CLIENT_ID,
                'client_secret': CLIENT_SECRET,
                'scope': 'https://graph.microsoft.com/.default'
            }
            try:
                response = requests.post(url, data=data)
                response.raise_for_status()
                js = response.json()
                self.token = js['access_token']
                # 提前5分钟视为过期，确保安全
                self.token_expires_at = time.time() + js.get('expires_in', 3600) - 300
                print("Token 已获取/刷新。")
            except Exception as e:
                print(f"获取 Token 失败: {e}")
                exit()

    @property
    def headers(self):
        self.get_valid_token()
        return {
            'Authorization': 'Bearer ' + self.token,
            'Content-Type': 'application/json'
        }

    def get_site_id(self):
        hostname = SITE_URL.split('/')[2]
        site_path = '/'.join(SITE_URL.split('/')[3:])
        api_url = f"{self.base_url}/sites/{hostname}:/{site_path}"
        response = requests.get(api_url, headers=self.headers)
        if response.status_code != 200:
            print(f"Error getting site ID: {response.text}")
            return None
        return response.json()['id']

    def get_drive_and_folder_id(self, site_id, folder_path):
        drive_url = f"{self.base_url}/sites/{site_id}/drive"
        drive_resp = requests.get(drive_url, headers=self.headers)
        if drive_resp.status_code != 200:
            print("无法获取 Drive ID")
            return None, None
        drive_id = drive_resp.json()['id']

        item_url = f"{self.base_url}/drives/{drive_id}/root:{folder_path}"
        item_resp = requests.get(item_url, headers=self.headers)
        if item_resp.status_code != 200:
            print(f"Error: 找不到路径 {folder_path}")
            return None, None
        return drive_id, item_resp.json()['id']

    def get_all_subfolders(self, drive_id, parent_item_id):
        """拉取所有文件夹，使用 minimal select 优化速度"""
        folders = []
        url = f"{self.base_url}/drives/{drive_id}/items/{parent_item_id}/children?$top=999&$select=id,name,folder"

        print("正在拉取文件夹列表...")
        while url:
            try:
                response = requests.get(url, headers=self.headers)
                data = response.json()

                for item in data.get('value', []):
                    # 过滤掉非文件夹，和纯数字文件夹(防止移动已创建的目标组)
                    if 'folder' in item and not item['name'].isdigit():
                        folders.append({'id': item['id'], 'name': item['name']})

                if len(folders) % 2000 == 0 and len(folders) > 0:
                    print(f"  已获取 {len(folders)} 个...")

                url = data.get('@odata.nextLink')
            except Exception as e:
                print(f"拉取中断: {e}")
                break

        print(f"列表获取完成，共 {len(folders)} 个文件夹。")
        return folders

    def custom_sort_key(self, folder_item):
        """
        核心排序逻辑：
        RJ0109 -> ('RJ', 109)
        PJK1987 -> ('PJK', 1987)
        """
        name = folder_item['name']
        # 正则：匹配 (开头的所有字母) + (后面的所有数字)
        match = re.match(r"^([A-Za-z]+)(\d+)", name)

        if match:
            prefix = match.group(1).upper()  # 字母部分转大写，确保 RJ 和 rj 排在一起
            number = int(match.group(2))  # 数字部分转整数，确保 2 < 10
            return (prefix, number)
        else:
            # 如果不符合格式（比如 "Backup"），则放到最后，或者按原名排序
            # (name, 0) 表示按字母排，数字权重为0
            return (name, 0)

    def create_folder(self, drive_id, parent_item_id, folder_name):
        url = f"{self.base_url}/drives/{drive_id}/items/{parent_item_id}/children"
        body = {
            "name": folder_name,
            "folder": {},
            "@microsoft.graph.conflictBehavior": "fail"
        }
        resp = requests.post(url, headers=self.headers, json=body)
        if resp.status_code == 201:
            return resp.json()['id']
        elif resp.status_code == 409:
            get_url = f"{self.base_url}/drives/{drive_id}/items/{parent_item_id}:/{folder_name}"
            return requests.get(get_url, headers=self.headers).json()['id']
        return None

    def execute_batch(self, batch_requests):
        if not batch_requests: return
        batch_url = "https://graph.microsoft.com/v1.0/$batch"
        try:
            requests.post(batch_url, headers=self.headers, json={"requests": batch_requests})
        except Exception as e:
            print(f"Batch Error: {e}")

    def run(self):
        print(">>> 开始执行自定义排序整理...")
        site_id = self.get_site_id()
        if not site_id: return

        drive_id, source_folder_id = self.get_drive_and_folder_id(site_id, TARGET_FOLDER_PATH)
        if not drive_id: return

        # 1. 获取列表
        all_folders = self.get_all_subfolders(drive_id, source_folder_id)

        # 2. 应用自定义排序
        print("正在进行自定义排序 (字母+数字大小)...")
        # key 传入上面的逻辑函数
        all_folders.sort(key=self.custom_sort_key)

        # 3. 分组
        chunk_size = 500
        total_chunks = math.ceil(len(all_folders) / chunk_size)

        print(f"排序完成。前5个示例: {[f['name'] for f in all_folders[:5]]}")
        print(f"将分为 {total_chunks} 个组。")

        for i in range(total_chunks):
            group_name = str(i + 1)
            chunk_items = all_folders[i * chunk_size: (i + 1) * chunk_size]

            print(f"\n正在处理第 {group_name} 组 ({len(chunk_items)} 个文件)...")

            target_folder_id = self.create_folder(drive_id, source_folder_id, group_name)
            if not target_folder_id: continue

            # Batch Process
            batch_requests = []
            for index, item in enumerate(chunk_items):
                req = {
                    "id": str(index),
                    "method": "PATCH",
                    "url": f"/drives/{drive_id}/items/{item['id']}",
                    "body": {"parentReference": {"id": target_folder_id}},
                    "headers": {"Content-Type": "application/json"}
                }
                batch_requests.append(req)

                if len(batch_requests) == 20:
                    self.execute_batch(batch_requests)
                    batch_requests = []
                    time.sleep(0.2)  # 防限流

            if batch_requests:
                self.execute_batch(batch_requests)

            print(f"  - 组 {group_name} 完成。")


if __name__ == '__main__':
    try:
        organizer = SharePointCustomSortBatch()
        organizer.run()
    except Exception as e:
        print(f"程序出错: {e}")
