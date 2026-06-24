# AWS.sb 面板

多实例 IP 监控与自动换IP管理面板。

## 功能

- 多实例管理（手动添加 / SGT 一键导入）
- IPv4 / IPv6 连通性检测（TCP 端口）
- 连续 3 次不通自动触发换IP
- 手动换IP按钮
- 换IP历史记录
- Bark 推送通知
- 账号密码登录保护
- 实时状态推送（WebSocket）

## 一键安装（Linux）

```bash
curl -sSL https://raw.githubusercontent.com/linglala/awsxzs/main/install.sh | sudo bash
```

或手动安装：

```bash
git clone https://github.com/YOUR_USERNAME/awssb-panel
cd awssb-panel
sudo bash install.sh
```

## 手动运行

```bash
pip install -r requirements.txt
python app.py
```

访问 `http://服务器IP:5000`，默认账号 `admin` / `admin123`

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `PORT` | 监听端口 | `5000` |
| `PANEL_USER` | 用户名 | `admin` |
| `PANEL_PASS` | 密码 | `admin123` |
| `SECRET_KEY` | Session 密钥 | 随机生成 |

## 如何添加实例

1. 打开 aws.sb，从 URL 获取 SGT Token
2. 面板里点「导入 SGT」自动导入所有实例
3. 或手动填写 Instance ID + Profile ID

## 项目结构

```
awssb-panel/
├── app.py              # 后端主程序
├── requirements.txt    # Python 依赖
├── install.sh          # 一键安装脚本
├── config.json         # 配置文件（运行后自动生成）
├── history.json        # 历史记录（运行后自动生成）
└── templates/
    ├── login.html      # 登录页
    └── index.html      # 主面板
```
