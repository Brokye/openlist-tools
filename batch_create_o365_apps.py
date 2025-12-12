import json
import requests
import webbrowser
import csv
import os  # 用于检查文件是否存在
from datetime import datetime, timedelta, timezone
from azure.identity import InteractiveBrowserCredential

# ================= 配置区域 =================
APP_DISPLAY_NAME = "o365"
SIGN_IN_AUDIENCE = "AzureADandPersonalMicrosoftAccount"
SECRET_LIFETIME_DAYS = 730
REQUIRED_PERMISSIONS = [
    "Application.ReadWrite.All",
    "Application.ReadWrite.OwnedBy",
    "Directory.ReadWrite.All",
    "Reports.Read.All"
]
CSV_FILENAME = "app_data.csv"  # 定义保存的文件名


# ===========================================

def main():
    print(">>> 正在启动 Azure 应用程序创建向导...")
    print(">>> 请在弹出的浏览器窗口中登录您的 Azure 管理员账户...")

    try:
        credential = InteractiveBrowserCredential()
        token_object = credential.get_token("https://graph.microsoft.com/.default")
        access_token = token_object.token
    except Exception as e:
        print(f"!!! 登录失败: {e}")
        return

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    print(">>> 正在获取租户信息...")
    me_resp = requests.get("https://graph.microsoft.com/v1.0/organization", headers=headers)
    if me_resp.status_code != 200:
        print(f"无法获取租户信息: {me_resp.text}")
        return
    tenant_id = me_resp.json()['value'][0]['id']
    print(f"    租户 ID: {tenant_id}")

    print(">>> 正在解析 API 权限 ID...")
    graph_sp_url = "https://graph.microsoft.com/v1.0/servicePrincipals"
    params = {"$filter": "appId eq '00000003-0000-0000-c000-000000000000'"}
    sp_resp = requests.get(graph_sp_url, headers=headers, params=params)

    if sp_resp.status_code != 200 or not sp_resp.json()['value']:
        print("!!! 无法找到 Microsoft Graph 服务主体信息。")
        return

    graph_sp_data = sp_resp.json()['value'][0]
    graph_app_id = graph_sp_data['appId']
    all_roles = graph_sp_data['appRoles']

    resource_access_list = []
    for perm_name in REQUIRED_PERMISSIONS:
        role = next((r for r in all_roles if r['value'] == perm_name), None)
        if role:
            resource_access_list.append({
                "id": role['id'],
                "type": "Role"
            })
            print(f"    + 已匹配权限: {perm_name}")
        else:
            print(f"    - 警告: 未找到权限 '{perm_name}'")

    print(f">>> 正在创建应用程序 '{APP_DISPLAY_NAME}'...")
    create_app_url = "https://graph.microsoft.com/v1.0/applications"
    app_payload = {
        "displayName": APP_DISPLAY_NAME,
        "signInAudience": SIGN_IN_AUDIENCE,
        "web": {
            "redirectUris": ["https://oauth.pstmn.io/v1/callback"]
        },
        "requiredResourceAccess": [
            {
                "resourceAppId": graph_app_id,
                "resourceAccess": resource_access_list
            }
        ]
    }

    create_resp = requests.post(create_app_url, headers=headers, json=app_payload)
    if create_resp.status_code not in [200, 201]:
        print(f"!!! 创建应用程序失败: {create_resp.text}")
        return

    app_data = create_resp.json()
    app_object_id = app_data['id']
    client_id = app_data['appId']

    print(">>> 正在生成 Client Secret (有效期 730 天)...")
    add_key_url = f"https://graph.microsoft.com/v1.0/applications/{app_object_id}/addPassword"

    end_date = datetime.now(timezone.utc) + timedelta(days=SECRET_LIFETIME_DAYS)
    key_payload = {
        "passwordCredential": {
            "displayName": "o365",
            "endDateTime": end_date.isoformat()
        }
    }

    key_resp = requests.post(add_key_url, headers=headers, json=key_payload)

    if key_resp.status_code not in [200, 201]:
        print(f"!!! 创建密钥失败: {key_resp.text}")
        secret_text = "生成失败"
    else:
        secret_text = key_resp.json()['secretText']

    # ================= CSV 保存逻辑开始 =================
    print(f">>> 正在写入本地文件 {CSV_FILENAME} ...")
    try:
        # 检查文件是否存在，以决定是否写入表头
        file_exists = os.path.isfile(CSV_FILENAME)

        # 使用 'a' (append) 模式打开，'utf-8-sig' 确保 Excel 打开时不乱码
        with open(CSV_FILENAME, mode='a', newline='', encoding='utf-8-sig') as csvfile:
            writer = csv.writer(csvfile)

            # 如果文件不存在，先写入标题行
            if not file_exists:
                writer.writerow(["Tenant ID", "Client ID", "Client Secret", "Created Time"])

            # 写入数据行
            writer.writerow([
                tenant_id,
                client_id,
                secret_text,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ])
        print(f"    √ 数据已追加保存至 {CSV_FILENAME}")
    except Exception as e:
        print(f"!!! 写入 CSV 失败: {e}")
    # ================= CSV 保存逻辑结束 =================

    print("\n" + "=" * 50)
    print("              应用程序创建成功")
    print("=" * 50)
    print(f"Tenant ID (租户ID)    : {tenant_id}")
    print(f"Client ID (应用ID)    : {client_id}")
    print(f"Client Secret (密码)  : {secret_text}")
    print(f"Secret 到期时间       : {end_date.strftime('%Y-%m-%d')}")
    print("=" * 50)
    print("注意：请立即保存 Client Secret，它不会再次显示！\n")

    print(">>> 正在生成管理员授权链接...")
    consent_url = (
        f"https://login.microsoftonline.com/{tenant_id}/adminconsent"
        f"?client_id={client_id}"
        f"&redirect_uri=https://oauth.pstmn.io/v1/callback"
    )

    print("为了让上述权限生效，您必须执行'管理员同意'。")
    print(f"请复制以下链接访问：\n{consent_url}")

    webbrowser.open(consent_url)


if __name__ == "__main__":
    main()
