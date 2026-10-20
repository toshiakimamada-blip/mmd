#!/usr/bin/env bash
# 固定した版の Node.js 24 系（npm 11.10 以上を同梱）で、渡されたコマンドを実行する。
#
# 使い方（frontend/ で実行する）:
#   ../tools/with-pinned-node.sh npm install <パッケージ>   # 依存を足す
#   ../tools/with-pinned-node.sh npm ci                      # ロックファイルの版だけを入れる
#
# なぜ要るか:
#   frontend/.npmrc の min-release-age（公開から 7 日未満の版を入れない設定）は、npm 11.10 以上でしか効かない。
#   開発用コンテナの npm は 10 系で、この設定を黙って無視する。そのため frontend/.npmrc の engine-strict で、
#   古い npm による install・ci・update を止めている。止まったときは、このスクリプトで実行し直す。
#
# Node.js の版とハッシュ値は、イメージのビルド（Dockerfile）と同じ値を使う。
# 値の出どころは Dockerfile の ARG（NODE_VERSION・NODE_SHA256_X64・NODE_SHA256_ARM64）だけにして、
# 開発とイメージで Node.js の版がずれないようにする。
#
# 初回だけ、公式の配布元（nodejs.org）から Node.js をダウンロードし、ハッシュ値を確かめて、
# キャッシュ（既定は ~/.cache/mmd-node。MMD_NODE_CACHE で変えられる）へ置く。2 回目からは再利用する。
set -euo pipefail

if [ "$#" -eq 0 ]; then
  echo "使い方: $0 <コマンド> [引数...]  （例: $0 npm install <パッケージ>）" >&2
  exit 2
fi

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dockerfile="${script_dir}/../Dockerfile"

# Dockerfile の「ARG 名=値」から、値を読む。
read_arg() {
  sed -n "s/^ARG $1=//p" "$dockerfile" | head -n 1
}

version="$(read_arg NODE_VERSION)"
case "$(uname -m)" in
  x86_64) arch="x64"; sha256="$(read_arg NODE_SHA256_X64)" ;;
  aarch64 | arm64) arch="arm64"; sha256="$(read_arg NODE_SHA256_ARM64)" ;;
  *) echo "未対応の CPU です: $(uname -m)" >&2; exit 1 ;;
esac
if [ -z "$version" ] || [ -z "$sha256" ]; then
  echo "Dockerfile から Node.js の版とハッシュ値を読めません: ${dockerfile}" >&2
  exit 1
fi

cache_root="${MMD_NODE_CACHE:-${XDG_CACHE_HOME:-${HOME}/.cache}/mmd-node}"
node_dir="${cache_root}/node-v${version}-linux-${arch}"

# Node.js を取得して展開する。途中で失敗したときは、置き場を作らない（次回にやり直せる）。
install_node() {
  work="$(mktemp -d "${cache_root}/download.XXXXXX")"
  # 失敗して止まったときも、作業用のフォルダーを残さない。
  trap 'rm -rf "${work}"' EXIT
  echo "Node.js ${version} を取得します（初回のみ）: ${node_dir}" >&2
  curl -fsSL -o "${work}/node.tar.xz" \
    "https://nodejs.org/dist/v${version}/node-v${version}-linux-${arch}.tar.xz"
  # ハッシュ値が合わなければ、ここで止まる（set -e）。
  echo "${sha256}  ${work}/node.tar.xz" | sha256sum -c - >&2
  mkdir "${work}/extract"
  tar -xJf "${work}/node.tar.xz" -C "${work}/extract" --strip-components=1
  mv "${work}/extract" "$node_dir"
  # このあと exec で置き換わり、EXIT の処理が動かないため、ここで片づける。
  rm -rf "${work}"
  trap - EXIT
}

if [ ! -x "${node_dir}/bin/node" ]; then
  mkdir -p "$cache_root"
  install_node
fi

PATH="${node_dir}/bin:${PATH}"
export PATH
exec "$@"
