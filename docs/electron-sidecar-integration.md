# Electron 嵌入 QwenPaw Sidecar 完整方案

## 一、当前架构分析

### 1.1 Sidecar 二进制产物

PyInstaller 构建的 `qwenpaw-backend` 目录（onedir 模式）包含两个可执行文件：

| 文件 | 用途 | 入口 |
|------|------|------|
| `qwenpaw-backend` (.exe) | HTTP 服务 (FastAPI + Uvicorn) | `src/qwenpaw/tauri/entry.py` |
| `qwenpaw` (.exe) | CLI 命令行工具 | `src/qwenpaw/tauri/cli_entry.py` |

CI 产物为 `qwenpaw-sidecar-{platform}-{arch}.zip`，已是**框架无关**的独立二进制包。

### 1.2 Tauri 集成现状（耦合点）

当前 `entry.py` 中有 **4 个 Tauri 强耦合点**：

```
┌──────────────────────────────────────────────────────────────┐
│  entry.py → main()                                           │
│                                                              │
│  1. _install_desktop_runtime()                               │
│     ├── QWENPAW_DESKTOP_APP=1         (Tauri 环境变量)      │
│     ├── ensure_desktop_cors_origins()  (硬编码 Tauri 源)     │
│     │   └── tauri://localhost                               │
│     │   └── https://tauri.localhost                         │
│     │   └── http://tauri.localhost                          │
│     └── _sync_loaded_qwenpaw_constant_cors_origins()        │
│                                                              │
│  2. install_sidecar_logging()                                │
│     └── 日志标签: "qwenpaw tauri sidecar"                    │
│                                                              │
│  3. _emit_backend_ready()                                    │
│     └── 协议: QWENPAW_BACKEND_READY {"port":N}             │
│        (Tauri Rust 端 events.rs 解析此协议)                  │
│                                                              │
│  4. console=False (PyInstaller spec)                         │
│     └── Windows 下无控制台窗口，适合 Tauri 后台运行          │
│        但独立运行时看不到任何输出                             │
└──────────────────────────────────────────────────────────────┘
```

### 1.3 Tauri 侧的数据流

```
Tauri Rust (backend.rs)
  │
  ├── spawn(qwenpaw-backend.exe)          启动 sidecar 进程
  ├── 读取 stdout 解析                    QWENPAW_BACKEND_READY {"port":N}
  ├── set_port_if_current(port)           存储端口到 BackendState
  │
  └── Tauri command: backend_port         前端 invoke("backend_port") → port
        │
        └── 前端 backendRuntime.ts        http://127.0.0.1:${port}
              │
              └── WebView navigate        跳转到后端托管的 /console 页面
```

---

## 二、解耦策略：客户端无关的 Sidecar

### 2.1 核心原则

**sidecar 二进制必须保持框架无关**。Tauri / Electron / 其他客户端的差异通过**环境变量和命令行参数**适配，而非硬编码在入口中。

### 2.2 改造 entry.py 支持多客户端

将 `src/qwenpaw/tauri/entry.py` 改造为通用的 `src/qwenpaw/sidecar/entry.py`：

```python
# src/qwenpaw/sidecar/entry.py（改造后）
"""Client-agnostic sidecar entry point for starting the Python backend."""

import argparse
import json
import logging
import multiprocessing as mp
import os
import socket
import sys

logger = logging.getLogger(__name__)

# ─── 客户端无关的常量 ───
READY_PREFIX = "QWENPAW_BACKEND_READY"  # 所有客户端统一协议

def parse_args():
    parser = argparse.ArgumentParser(description="QwenPaw Backend Sidecar")
    parser.add_argument(
        "--client", default="generic",
        choices=["tauri", "electron", "generic"],
        help="Desktop client type (default: generic)",
    )
    parser.add_argument(
        "--port", type=int, default=0,
        help="Fixed port (0 = auto-assign with stable reuse)",
    )
    parser.add_argument(
        "--log-level", default="info",
        choices=["critical", "error", "warning", "info", "debug", "trace"],
        help="Log level (default: info)",
    )
    parser.add_argument(
        "--cors-origins", default="",
        help="Additional CORS origins (comma-separated)",
    )
    parser.add_argument(
        "--standalone", action="store_true",
        help="Standalone mode: enable console output, skip desktop runtime",
    )
    parser.add_argument(
        "--health-check", action="store_true",
        help="After startup, probe /api/version and exit 0/1",
    )
    return parser.parse_args()


def _ensure_utf8_stdio():
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def _install_certifi_env():
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi
    except Exception:
        return
    cert_file = certifi.where()
    if cert_file and os.path.isfile(cert_file):
        os.environ.setdefault("SSL_CERT_FILE", cert_file)
        os.environ.setdefault("REQUESTS_CA_BUNDLE", cert_file)
        os.environ.setdefault("CURL_CA_BUNDLE", cert_file)


def _install_cors(client: str, extra_origins: str):
    """Set QWENPAW_CORS_ORIGINS based on client type."""
    # 基础源（各客户端通用）
    origins = []

    # 客户端特有源
    client_origins = {
        "tauri": [
            "tauri://localhost",
            "https://tauri.localhost",
            "http://tauri.localhost",
        ],
        "electron": [
            "file://",
            "app://localhost",
            "http://localhost:*",      # Electron dev server
            "http://127.0.0.1:*",
        ],
        "generic": [],
    }
    origins.extend(client_origins.get(client, []))

    # 环境变量传入的额外源
    env_origins = os.environ.get("QWENPAW_CORS_ORIGINS", "")
    for o in env_origins.split(","):
        o = o.strip()
        if o and o not in origins:
            origins.append(o)

    # --cors-origins 参数传入
    if extra_origins:
        for o in extra_origins.split(","):
            o = o.strip()
            if o and o not in origins:
                origins.append(o)

    if origins:
        os.environ["QWENPAW_CORS_ORIGINS"] = ",".join(origins)


def _emit_backend_ready(port: int):
    """统一就绪协议，所有客户端解析同一格式。"""
    payload = json.dumps({"port": port}, separators=(",", ":"))
    print(f"{READY_PREFIX} {payload}", flush=True)


def _run_health_check(host: str, port: int) -> bool:
    """Probe /api/version to verify the server is alive."""
    import urllib.request
    import urllib.error
    url = f"http://{host}:{port}/api/version"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            version = data.get("version", "?")
            print(f"Health check PASSED: {url} → v{version}", flush=True)
            return True
    except Exception as e:
        print(f"Health check FAILED: {url} → {e}", file=sys.stderr)
        return False


def _run_backend_server(host: str, fixed_port: int, log_level: str):
    import uvicorn
    from qwenpaw.config.utils import write_last_api
    from qwenpaw.constant import LOG_LEVEL_ENV, WORKING_DIR
    from qwenpaw.utils.logging import (
        SuppressPathAccessLogFilter,
        setup_logger,
    )
    from qwenpaw.utils.port import get_stable_port, write_port_file

    os.environ[LOG_LEVEL_ENV] = log_level
    os.environ.pop("QWENPAW_RELOAD_MODE", None)
    setup_logger(log_level)

    logging.getLogger("uvicorn.access").addFilter(
        SuppressPathAccessLogFilter(["/console/push-messages"]),
    )

    port_file = str(WORKING_DIR / "desktop_port")

    if fixed_port > 0:
        port = fixed_port
        reused_socket = None
        backend_socket = None
    else:
        port, reused_socket = get_stable_port(port_file, host)

    config = uvicorn.Config(
        "qwenpaw.app._app:app",
        host=host,
        port=fixed_port if fixed_port > 0 else 0,
        reload=False,
        workers=1,
        log_level=log_level,
    )

    if reused_socket:
        backend_socket = reused_socket
    elif backend_socket is None:
        backend_socket = config.bind_socket()

    try:
        if fixed_port <= 0:
            port = _socket_port(backend_socket)
        write_port_file(port_file, port)
        write_last_api(host, port)
        _emit_backend_ready(port)
        uvicorn.Server(config).run(sockets=[backend_socket])
    except Exception:
        if backend_socket:
            backend_socket.close()
        raise


def _socket_port(sock: socket.socket) -> int:
    address = sock.getsockname()
    if not isinstance(address, tuple) or len(address) < 2:
        raise RuntimeError(f"unexpected socket address: {address!r}")
    return int(address[1])


def main() -> None:
    args = parse_args()
    _ensure_utf8_stdio()

    # Desktop runtime (CORS + env) — skip in standalone
    if not args.standalone:
        os.environ.setdefault("QWENPAW_DESKTOP_APP", "1")
        _install_cors(args.client, args.cors_origins)
        # Sync CORS to constant if already imported
        constant_mod = sys.modules.get("qwenpaw.constant")
        if constant_mod is not None:
            constant_mod.CORS_ORIGINS = os.environ.get(
                "QWENPAW_CORS_ORIGINS", ""
            ).strip()

    # File logging (both desktop and standalone)
    from qwenpaw.constant import WORKING_DIR
    log_file = WORKING_DIR / "desktop.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    # ... (reuse install_sidecar_logging or simplified version)

    _install_certifi_env()

    # Auto-init if no config
    config_path = WORKING_DIR / "config.json"
    if not config_path.exists():
        from qwenpaw.cli.init_cmd import init_cmd
        init_cmd.main(args=["--defaults", "--accept-security"],
                      standalone_mode=False)

    _run_backend_server("127.0.0.1", args.port, args.log_level)


if __name__ == "__main__":
    mp.freeze_support()
    main()
```

### 2.3 兼容旧入口

保留 `src/qwenpaw/tauri/entry.py` 作为薄包装器，向后兼容 CI 和 Tauri：

```python
# src/qwenpaw/tauri/entry.py（保留兼容）
"""Backward-compatible Tauri entry — delegates to the generic sidecar."""
import sys
sys.argv.extend(["--client", "tauri"])  # 追加默认客户端标识
from qwenpaw.sidecar.entry import main
import multiprocessing as mp

if __name__ == "__main__":
    mp.freeze_support()
    main()
```

---

## 三、Electron 嵌入实现

### 3.1 目录结构

```
electron-app/
├── package.json
├── electron-builder.yml
├── src/
│   ├── main.ts              # Electron 主进程
│   ├── sidecar.ts           # Sidecar 生命周期管理
│   ├── preload.ts           # 安全桥接
│   └── renderer.ts          # 渲染进程
├── resources/
│   └── qwenpaw-backend/     # sidecar 二进制 (从 CI 解压)
│       ├── qwenpaw-backend.exe
│       ├── qwenpaw.exe
│       └── ... (依赖 DLL/so)
└── tsconfig.json
```

### 3.2 Sidecar 生命周期管理 (sidecar.ts)

```typescript
// src/sidecar.ts
import { spawn, ChildProcess } from "child_process";
import { app } from "electron";
import * as path from "path";
import * as http from "http";

const READY_PREFIX = "QWENPAW_BACKEND_READY ";

export interface SidecarInfo {
  process: ChildProcess;
  port: number;
  pid: number;
}

export class SidecarManager {
  private child: ChildProcess | null = null;
  private port: number | null = null;
  private readyPromise: Promise<number> | null = null;

  /**
   * 获取 sidecar 可执行文件路径。
   * 开发模式: resources/qwenpaw-backend/
   * 打包后: process.resourcesPath/qwenpaw-backend/
   */
  private getExecutablePath(): string {
    const exe = process.platform === "win32"
      ? "qwenpaw-backend.exe"
      : "qwenpaw-backend";
    const base = app.isPackaged
      ? process.resourcesPath
      : path.join(__dirname, "..", "resources");
    return path.join(base, "qwenpaw-backend", exe);
  }

  /**
   * 启动 sidecar 进程。
   * 返回 Promise<number>，resolve 时携带分配的端口号。
   */
  start(): Promise<number> {
    if (this.readyPromise) return this.readyPromise;

    this.readyPromise = new Promise((resolve, reject) => {
      const exe = this.getExecutablePath();
      const sidecarDir = path.dirname(exe);

      // 环境变量
      const env = {
        ...process.env,
        PYTHONUTF8: "1",
        PYTHONIOENCODING: "utf-8",
        PYTHONUNBUFFERED: "1",
        PYTHONFAULTHANDLER: "1",
        QWENPAW_DESKTOP_APP: "1",
        // 设置 PATH 使 sidecar 目录下的 DLL 可被发现
        ...(process.platform === "win32"
          ? { Path: `${sidecarDir};${process.env.Path || ""}` }
          : { PATH: `${sidecarDir}:${process.env.PATH || ""}` }),
      };

      this.child = spawn(exe, [
        "--client", "electron",
        "--log-level", "info",
      ], {
        cwd: sidecarDir,
        env,
        stdio: ["ignore", "pipe", "pipe"],
        // Windows: 不打开新控制台窗口
        windowsHide: true,
      });

      let stderrBuffer = "";

      // 解析 stdout 中的就绪信号
      this.child.stdout?.on("data", (chunk: Buffer) => {
        const text = chunk.toString("utf-8");
        console.log(`[sidecar stdout] ${text.trim()}`);

        for (const line of text.split("\n")) {
          const trimmed = line.trim();
          if (trimmed.startsWith(READY_PREFIX)) {
            try {
              const payload = JSON.parse(trimmed.slice(READY_PREFIX.length));
              this.port = payload.port;
              console.log(`[sidecar] ready on port ${this.port}`);
              resolve(this.port);
            } catch (e) {
              console.error("[sidecar] failed to parse ready payload:", e);
            }
          }
        }
      });

      // 捕获 stderr
      this.child.stderr?.on("data", (chunk: Buffer) => {
        const text = chunk.toString("utf-8");
        console.error(`[sidecar stderr] ${text.trim()}`);
        stderrBuffer += text;
        if (stderrBuffer.length > 4000) {
          stderrBuffer = stderrBuffer.slice(-4000);
        }
      });

      // 进程异常退出
      this.child.on("exit", (code, signal) => {
        console.warn(`[sidecar] exited code=${code} signal=${signal}`);
        if (this.readyPromise) {
          reject(new Error(
            `Sidecar exited: code=${code}, signal=${signal}\n` +
            `Last stderr:\n${stderrBuffer.slice(-2000)}`
          ));
        }
        this.child = null;
        this.port = null;
        this.readyPromise = null;
      });

      this.child.on("error", (err) => {
        console.error("[sidecar] spawn error:", err);
        reject(err);
        this.readyPromise = null;
      });
    });

    return this.readyPromise;
  }

  /** 获取当前 sidecar 端口 */
  getPort(): number | null {
    return this.port;
  }

  /** 健康检查：GET /api/version */
  async healthCheck(): Promise<boolean> {
    if (!this.port) return false;
    return new Promise((resolve) => {
      const req = http.get(
        `http://127.0.0.1:${this.port}/api/version`,
        { timeout: 5000 },
        (res) => {
          resolve(res.statusCode === 200);
          res.resume();
        },
      );
      req.on("error", () => resolve(false));
      req.on("timeout", () => {
        req.destroy();
        resolve(false);
      });
    });
  }

  /** 停止 sidecar 进程 */
  stop(): void {
    if (this.child) {
      console.log(`[sidecar] stopping pid=${this.child.pid}`);
      // 优先 SIGTERM，超时 SIGKILL
      this.child.kill("SIGTERM");
      setTimeout(() => {
        if (this.child && !this.child.killed) {
          this.child.kill("SIGKILL");
        }
      }, 5000);
      this.child = null;
      this.port = null;
      this.readyPromise = null;
    }
  }

  /** 重启 sidecar */
  async restart(): Promise<number> {
    this.stop();
    return this.start();
  }
}
```

### 3.3 Electron 主进程 (main.ts)

```typescript
// src/main.ts
import { app, BrowserWindow, ipcMain } from "electron";
import * as path from "path";
import { SidecarManager } from "./sidecar";

const sidecar = new SidecarManager();
let mainWindow: BrowserWindow | null = null;

async function createWindow(port: number) {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  // 方式 A: 直接加载后端托管的 console (推荐，与 Tauri 一致)
  const backendUrl = `http://127.0.0.1:${port}/console`;
  await mainWindow.loadURL(backendUrl);

  // 方式 B: 加载本地 HTML，通过 API 调用后端 (可选)
  // await mainWindow.loadFile(path.join(__dirname, "..", "renderer", "index.html"));

  mainWindow.on("closed", () => {
    mainWindow = null;
  });
}

// ─── IPC 接口暴露给渲染进程 ───
ipcMain.handle("sidecar:port", () => sidecar.getPort());
ipcMain.handle("sidecar:restart", () => sidecar.restart());
ipcMain.handle("sidecar:health", () => sidecar.healthCheck());

app.whenReady().then(async () => {
  try {
    const port = await sidecar.start();
    console.log(`Sidecar ready on port ${port}`);
    await createWindow(port);
  } catch (err) {
    console.error("Failed to start sidecar:", err);
    app.quit();
  }
});

app.on("window-all-closed", () => {
  sidecar.stop();
  app.quit();
});

app.on("before-quit", () => {
  sidecar.stop();
});
```

### 3.4 Preload 脚本 (preload.ts)

```typescript
// src/preload.ts
import { contextBridge, ipcRenderer } from "electron";

contextBridge.exposeInMainWorld("sidecar", {
  getPort: (): Promise<number | null> => ipcRenderer.invoke("sidecar:port"),
  restart: (): Promise<number> => ipcRenderer.invoke("sidecar:restart"),
  healthCheck: (): Promise<boolean> => ipcRenderer.invoke("sidecar:health"),
});
```

### 3.5 electron-builder 打包配置

```yaml
# electron-builder.yml
appId: io.agentscope.qwenpaw.desktop
productName: QwenPaw Desktop
directories:
  output: dist/electron

files:
  - src/**/*
  - "package.json"

extraResources:
  - from: "resources/qwenpaw-backend"
    to: "qwenpaw-backend"
    filter:
      - "**/*"

win:
  target: nsis
  icon: "../scripts/pack/assets/icon.ico"

mac:
  target: dmg
  icon: "../scripts/pack/assets/icon.icns"
  hardenedRuntime: true

nsis:
  oneClick: false
  allowToChangeInstallationDirectory: true
  installerIcon: "../scripts/pack/assets/icon.ico"
  languages:
    - English
    - SimplifiedChinese
```

---

## 四、CORS 处理差异对比

| 维度 | Tauri | Electron |
|------|-------|----------|
| 前端协议 | `tauri://localhost` | `file://` 或 `http://localhost:PORT` (dev) |
| 同源策略 | Tauri 使用自定义协议 | Electron 可使用 `webSecurity: false` 或同源 |
| 后端 CORS | 需要 `tauri://localhost` | 需要 `file://` 或 Electron dev server 源 |
| 推荐方案 | **后端托管 console** (已有) | **后端托管 console** (已有) |

**推荐方式**：Electron 直接 `loadURL("http://127.0.0.1:${port}/console")`。
这样前端与后端同源，**无需配置 CORS**，且与 Tauri 方案完全一致。

如果必须加载本地 HTML（跨域调用），则在 `sidecar --cors-origins` 中传入 Electron 的源。

---

## 五、就绪协议 (Ready Protocol)

所有客户端使用统一协议：

```
STDOUT: QWENPAW_BACKEND_READY {"port":54321}
```

| 客户端 | 解析方 | 实现 |
|--------|--------|------|
| Tauri | Rust `events.rs` | `ready_port_from_stdout()` — 已有 |
| Electron | TS `sidecar.ts` | stdout `data` 事件解析 — 见 §3.2 |
| Generic/测试 | Shell/Python | `grep "QWENPAW_BACKEND_READY"` 即可 |

---

## 六、构建流程适配

### 6.1 现有 CI 无需修改

`build-backend-sidecar.yml` 已产出框架无关的 zip：

```
qwenpaw-sidecar-windows-x64.zip
qwenpaw-sidecar-macos-arm64.zip
```

Electron 项目直接下载 zip → 解压到 `resources/qwenpaw-backend/` 即可。

### 6.2 Electron 项目构建脚本

```json
// package.json scripts
{
  "scripts": {
    "postinstall": "node scripts/download-sidecar.js",
    "dev": "electron .",
    "build": "electron-builder",
    "build:win": "electron-builder --win",
    "build:mac": "electron-builder --mac"
  }
}
```

```javascript
// scripts/download-sidecar.js
// 从 GitHub Actions artifact 或 Release 下载 sidecar zip
const { execSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const SIDE_DIR = path.join(__dirname, "..", "resources", "qwenpaw-backend");

if (fs.existsSync(path.join(SIDE_DIR, "qwenpaw-backend.exe")) ||
    fs.existsSync(path.join(SIDE_DIR, "qwenpaw-backend"))) {
  console.log("Sidecar already present");
  process.exit(0);
}

// 开发模式: 从本地 PyInstaller 构建产物复制
const localDist = path.join(__dirname, "..", "..", "dist", "pyinstaller", "qwenpaw-backend");
if (fs.existsSync(localDist)) {
  fs.cpSync(localDist, SIDE_DIR, { recursive: true });
  console.log(`Copied from ${localDist}`);
} else {
  console.error(
    "Sidecar not found. Run build_pyinstaller.ps1 first, " +
    "or set SIDE_URL to download from CI."
  );
  process.exit(1);
}
```

---

## 七、独立测试方案

改造后的 sidecar 支持三种独立测试模式：

### 7.1 直接运行（有控制台）

```powershell
# Windows
.\dist\pyinstaller\qwenpaw-backend\qwenpaw-backend.exe --standalone --port 8088
```

- `--standalone` 跳过桌面运行时设置（CORS、环境变量等）
- `--port 8088` 固定端口
- 控制台可见日志输出

### 7.2 健康检查

```powershell
# 启动 + 自动验证
.\dist\pyinstaller\qwenpaw-backend\qwenpaw-backend.exe --standalone --port 8088 --health-check
```

启动后 sidecar 自身会 `GET /api/version`，通过退出码判断服务是否正常。

### 7.3 CLI 工具独立使用

```powershell
# 查询服务版本
.\dist\pyinstaller\qwenpaw-backend\qwenpaw.exe --version

# 诊断
.\dist\pyinstaller\qwenpaw-backend\qwenpaw.exe doctor

# 管理 skills/channels 等
.\dist\pyinstaller\qwenpaw-backend\qwenpaw.exe skills list
```

---

## 八、PyInstaller spec 改造建议

当前 `console=False` 在 Windows 下会隐藏控制台。为了让 `--standalone` 模式有输出，
同时保持 Tauri/Electron 嵌入时的无窗口行为，建议：

### 方案 A：改为 console=True（推荐）

将 spec 中 `console=False` 改为 `console=True`：

```python
backend_exe = EXE(
    ...
    console=True,          # 改为 True
    ...
)
```

- **嵌入时**：Electron 用 `windowsHide: true`，Tauri 用 `creation_flags` 隐藏窗口
- **独立时**：控制台可见，可直接查看日志

### 方案 B：双 spec（备选）

维护两个 spec 文件，一个给嵌入用（console=False），一个给独立发行（console=True）。
维护成本高，不推荐。

---

## 九、完整对比：Tauri vs Electron 嵌入

| 维度 | Tauri | Electron |
|------|-------|----------|
| **sidecar 启动** | Rust `tauri_plugin_shell` spawn | Node.js `child_process.spawn` |
| **就绪信号解析** | Rust `events.rs` 解析 stdout | TS `sidecar.ts` 解析 stdout |
| **端口传递** | `tauri::command` invoke → 前端 | `ipcMain.handle` invoke → 前端 |
| **前端加载** | `navigate(http://127.0.0.1:port/console)` | `loadURL(http://127.0.0.1:port/console)` |
| **进程停止** | `CommandChild::kill()` | `child.kill("SIGTERM")` |
| **窗口隐藏** | `console=False` + Tauri 后台 | `windowsHide: true` |
| **CORS** | `tauri://localhost` 等 | `file://` 或同源（推荐同源加载） |
| **打包** | `bundle.resources` | `extraResources` |
| **健康检查** | Tauri 端未实现 | `http.get(/api/version)` |

---

## 十、实施步骤清单

### Phase 1: Sidecar 解耦（Python 端）

1. 新建 `src/qwenpaw/sidecar/` 目录，迁移 `entry.py` → 通用入口
2. 保留 `src/qwenpaw/tauri/entry.py` 作为兼容包装器
3. 增加 `--client`, `--standalone`, `--port`, `--cors-origins`, `--health-check` 参数
4. spec 文件 `console=False` → `console=True`
5. 验证 Tauri 嵌入不受影响（回归测试）

### Phase 2: Electron 客户端

1. 创建 `electron-app/` 项目骨架
2. 实现 `SidecarManager` (sidecar.ts)
3. 实现主进程 (main.ts) + preload (preload.ts)
4. 配置 electron-builder 打包
5. 编写 `download-sidecar.js` 脚本

### Phase 3: CI 适配

1. 现有 `build-backend-sidecar.yml` 已输出 zip，无需修改
2. 新增 Electron 构建 workflow（下载 sidecar zip → electron-builder）
3. 可选：sidecar zip 发布到 GitHub Release

### Phase 4: 独立测试

1. 验证 `qwenpaw-backend.exe --standalone` 独立运行
2. 验证 `qwenpaw-backend.exe --health-check` 自检
3. 验证 `qwenpaw.exe` CLI 独立使用
