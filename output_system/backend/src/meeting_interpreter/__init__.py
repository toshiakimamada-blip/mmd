"""会議通訳アプリのアプリサーバー。

画面のファイルの配信・HTTP の経路・WebSocket の経路を、1 つのプロセスで受け持つ。
"""

# 画面のヘッダと API の表題に出すアプリの名前。
# 画面側（frontend/src/appInfo.ts）の APP_NAME と同じ値にそろえる。
APP_NAME = "会議通訳"
