# syntax=docker/dockerfile:1
#
# 会議通訳アプリのイメージ。アプリサーバー（Python と FastAPI）と、ビルドした画面（React）を 1 つにまとめる。
#
# 段の構成
#   certs          社内の証明書の受け口（既定は空）
#   base           ubuntu:24.04 に、社内の証明書を任意で登録した共通の土台
#   frontend-build 画面をビルドする段（Node.js を入れる。最終のイメージには残さない）
#   backend-build  サーバーの依存を入れる段（uv を入れる。最終のイメージには残さない）
#   runtime        実行する段（Python と、ビルド済みの依存・画面だけを持つ）
#
# ビルドの文脈は output_system/ にする。
#   docker compose build
# 社内の証明書は、docker-compose.yml が、追加の文脈 certs として渡す。
# docker compose を使わずにビルドする場合は、証明書が無い環境ならそのまま、
# ある環境なら --build-context certs=<証明書のフォルダー> を付ける。

# ---------------------------------------------------------------------------
# 社内の証明書の受け口。
# 既定は空の段。docker-compose.yml の追加の文脈 certs（証明書のフォルダー）が、同じ名前のこの段を置き換える。
# 証明書が無い環境でも、空のフォルダーが渡る（または、この空の段のまま）ため、ビルドは成功する。
# ---------------------------------------------------------------------------
FROM scratch AS certs

# ---------------------------------------------------------------------------
# 共通の土台。
# 開発環境の土台（Ubuntu 24.04）とそろえる。違うと、apt のパッケージの版が変わって動かないことがある。
# ---------------------------------------------------------------------------
FROM ubuntu:24.04 AS base

ENV DEBIAN_FRONTEND=noninteractive

# ca-certificates: 公式の配布元（PyPI・npm・nodejs.org）へ HTTPS でつなぐために要る。
# openssl: 社内の証明書が DER 形式のとき、PEM 形式へ変換するために要る。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates openssl \
    && rm -rf /var/lib/apt/lists/*

# 社内のネットワークが通信を検査する場合の証明書を、システムの証明書の保管先へ登録する（任意）。
# 証明書が 1 つも無ければ、何もしない。PEM 形式はそのまま、DER 形式は PEM 形式へ変換して登録する。
COPY --from=certs / /tmp/custom-certs/
RUN set -eu; \
    count=0; \
    for file in /tmp/custom-certs/*.cer /tmp/custom-certs/*.crt /tmp/custom-certs/*.pem; do \
      [ -f "$file" ] || continue; \
      name="$(basename "${file%.*}")"; \
      destination="/usr/local/share/ca-certificates/custom-${name}.crt"; \
      if grep -q "BEGIN CERTIFICATE" "$file"; then \
        cp "$file" "$destination"; \
      else \
        openssl x509 -inform DER -in "$file" -out "$destination"; \
      fi; \
      count=$((count + 1)); \
    done; \
    if [ "$count" -gt 0 ]; then update-ca-certificates; fi; \
    echo "registered ${count} custom CA certificate(s)"; \
    rm -rf /tmp/custom-certs

# ---------------------------------------------------------------------------
# 画面のビルド。Node.js は、版とハッシュ値を固定して、公式の配布元から入れる。
# ---------------------------------------------------------------------------
FROM base AS frontend-build

# Node.js 24 系（LTS）。npm 11.10 以上が同梱されている版を選ぶ（frontend/.npmrc の min-release-age に必要）。
# tools/with-pinned-node.sh も、下の 3 つの ARG（NODE_VERSION・NODE_SHA256_X64・NODE_SHA256_ARM64）を
# 読む。開発用コンテナでも同じ版の Node.js で依存を足すため。ARG の名前を変えるときは、スクリプトも直す。
ARG NODE_VERSION=24.21.0
# 配布元の SHASUMS256.txt に載っている、アーカイブの SHA-256。版を上げるときは、一緒に更新する。
ARG NODE_SHA256_X64=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6
ARG NODE_SHA256_ARM64=6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2
# BuildKit が、ビルドする CPU の種類（amd64 または arm64）を入れる。
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl xz-utils \
    && rm -rf /var/lib/apt/lists/*

RUN set -eu; \
    case "${TARGETARCH}" in \
      amd64) arch=x64; sha256="${NODE_SHA256_X64}" ;; \
      arm64) arch=arm64; sha256="${NODE_SHA256_ARM64}" ;; \
      *) echo "unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/node.tar.xz \
      "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz"; \
    echo "${sha256}  /tmp/node.tar.xz" | sha256sum -c -; \
    mkdir -p /opt/node; \
    tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1; \
    rm /tmp/node.tar.xz

# NODE_EXTRA_CA_CERTS: Node.js は、システムの証明書の保管先を使わないため、社内の証明書を渡す。
ENV PATH=/opt/node/bin:${PATH} \
    NODE_EXTRA_CA_CERTS=/etc/ssl/certs/ca-certificates.crt

WORKDIR /build

# 依存の定義だけを先にコピーして npm ci を実行する。ソースを変えても、依存の層を使い回せる。
# npm ci は、package-lock.json に記録した版だけを入れる（版を新しく決めない）。
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
RUN npm ci

COPY frontend/ ./
# 型チェックとビルド。成果物は /build/dist に出る。
RUN npm run build

# ---------------------------------------------------------------------------
# サーバーの依存を入れる。uv は、版を固定して入れる。
# ---------------------------------------------------------------------------
FROM base AS backend-build

ARG UV_VERSION=0.12.21

RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

# PIP_CERT・SSL_CERT_FILE・UV_NATIVE_TLS: pip と uv に、システムの証明書の保管先（社内の証明書を含む）を使わせる。
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    UV_NATIVE_TLS=1

RUN python3 -m venv /opt/uv-venv \
    && /opt/uv-venv/bin/pip install --no-cache-dir "uv==${UV_VERSION}"

# UV_PROJECT_ENVIRONMENT: 依存の入れ先。実行する段へ、この場所のままコピーする。
# UV_PYTHON と UV_PYTHON_DOWNLOADS: Ubuntu の Python を使い、uv に別の Python を入れさせない。
# UV_COMPILE_BYTECODE: 依存を事前にコンパイルして、起動を速くする。
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /build

# --locked: uv.lock と pyproject.toml がずれていたら失敗させる。ロックファイルの版だけを入れる。
# --no-dev: テスト用の依存を入れない。
COPY backend/pyproject.toml backend/uv.lock backend/.python-version ./
RUN /opt/uv-venv/bin/uv sync --locked --no-dev --no-install-project

# ---------------------------------------------------------------------------
# 実行する段。
# ---------------------------------------------------------------------------
FROM base AS runtime

# 実行に要るのは Python だけ。pip・venv・コンパイラは入れない。
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 \
    && rm -rf /var/lib/apt/lists/*

# 一般の利用者（root 以外）で動かす。
# 2 つの置き場は、名前付きボリュームの置き場所にもなる。
# 新しい名前付きボリュームは、イメージにある置き場の持ち主を引き継ぐため、ここで持ち主を app にしておく。
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /app /var/lib/mmd/records /var/lib/mmd/models \
    && chown app:app /var/lib/mmd/records /var/lib/mmd/models

COPY --from=backend-build /opt/venv /opt/venv
COPY backend/src /app/src
# アプリに同梱する設定ファイル（調整値と AI モデルの定義）。置き場は MMD_CONFIG_DIR。
COPY config /app/config
COPY --from=frontend-build /build/dist /app/static

# PYTHONPATH: アプリのコードの場所（アプリは、パッケージとしては入れない）。
# PYTHONDONTWRITEBYTECODE: 読み取り専用のコードの置き場へ、コンパイルの結果を書こうとしない。
# PYTHONUNBUFFERED: ログを、すぐにコンテナの標準出力へ出す。
# SSL_CERT_FILE・SSL_CERT_DIR: AI モデルの入手（HTTPS）が、システムの証明書の保管先を読むように指す。
#   社内のネットワークの証明書は、base の段で、この保管先に登録済み。certifi の証明書の一覧は使わず、
#   書き換えもしない。Python の TLS の文脈（ssl.create_default_context）は、この 2 つを読む。
# MMD_*: 動かす場所で変わる値の既定。docker-compose.yml の environment で上書きできる。
ENV PATH=/opt/venv/bin:${PATH} \
    PYTHONPATH=/app/src \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_DIR=/etc/ssl/certs \
    MMD_PORT=3001 \
    MMD_STATIC_DIR=/app/static \
    MMD_CONFIG_DIR=/app/config \
    MMD_RECORDS_DIR=/var/lib/mmd/records \
    MMD_MODELS_DIR=/var/lib/mmd/models

USER app
WORKDIR /app

# 待ち受けのポート。コンテナの外へ公開する範囲は、docker-compose.yml の ports が決める。
EXPOSE 3001

# コンテナの死活の確認。docker compose ps に、正常かどうかが出る。
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-m", "meeting_interpreter.healthcheck"]

CMD ["python", "-m", "meeting_interpreter"]
