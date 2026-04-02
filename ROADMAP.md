# PyX Roadmap

> Last updated: 2026-04-02

## 长期目标：用 PyX 重写 rENDER 浏览器引擎

[rENDER](https://github.com/elyar-adil/rENDER) 是一个纯 Python 浏览器引擎，涵盖 HTML 解析、CSS 级联布局、JavaScript 执行和像素级渲染，目前依赖 PyQt6 作为渲染后端。

**本 Roadmap 的终极目标**：让 PyX 成熟到足以重写 rENDER，并以**调用原生平台 API**（Win32/GDI、X11/xcb、Wayland+EGL）替代 Qt，从而证明 PyX 可用于实现生产级 GUI 应用。

---

## 当前状态（Phase 1 完成，Phase 2 进行中）

- ✅ 静态子集检查器（注解约束、变量类型稳定性、反射调用限制）
- ✅ LLVM IR 代码生成（`int` 函数、算术、函数调用、递归、基础 `if`）
- ✅ `pyx build` 构建产物：`.ll` + 可选 `.o`

---

## Phase 2：类型系统与语义补全（进行中，目标：2026 Q2）

### 目标
扩展可编译语法覆盖，减少"能检查不能编译"的差距。

### 计划项
1. **控制流增强**
   - `if/elif/else` 的分支合流（phi 节点）
   - `while` 基础支持
2. **变量模型增强**
   - 支持同类型重赋值（SSA + alloca/store 降级策略）
3. **类型能力增强**
   - `bool`、`float` 一等支持
   - `int | float` 的最小 union 方案（tagged lowering）
4. **错误体验**
   - 统一 analyzer / compiler 的报错格式与错误码

### 已落地（2026-04-01）
- ✅ `while` 基础支持
- ✅ 同类型重赋值（基于 `alloca/load/store`）
- ✅ `bool` / `float` 一等编译支持

### 完成标准
- 至少 20 个端到端用例通过（check + build）。

---

## Phase 3：数据结构与模块系统（目标：2026 Q3）

### 目标
进入"可用于小型真实项目"的阶段，为 FFI 和字符串处理打基础。

### 计划项
1. **字符串类型**
   - `str` 的原生布局（ptr + len 或 null-terminated 可切换）
   - 基础操作：索引、切片、拼接、比较、UTF-8 编解码
2. **容器类型**
   - `list[T]` 的原生布局与常用操作（len / append / index）
   - `dict[K, V]` 的哈希表实现（浏览器需要大量映射）
3. **`class` / `dataclass` 支持**
   - 受限 `class` 到 struct 的 lowering（DOM 节点、CSS 规则都是结构体树）
   - 方法调用的静态分派
4. **模块系统**
   - 跨文件符号解析与链接
   - `import` 子集（同包模块，无动态 import）
5. **Python fallback 桥接**
   - 动态区块运行时桥（允许渐进迁移）

### 完成标准
- 支持包含多模块 + 容器操作 + 自定义类型的示例项目编译。

---

## Phase 4：原生 FFI 与平台绑定（目标：2026 Q4）

### 目标
让 PyX 能直接调用 C ABI，并封装 Windows / Linux 的原生 GUI API，**替代 Qt**。

### 计划项
1. **C ABI 互操作（FFI 核心）**
   - `extern "C"` 函数声明语法（cdecl / stdcall）
   - 原始指针类型：`ptr[T]`、`void_ptr`
   - 结构体与 C struct 的内存布局对齐
   - `unsafe` 块标记（隔离不安全操作）
   - 链接器指令：`#link "user32"` / `#link "X11"`
2. **Windows 平台绑定（`pyx.os.win32`）**
   - 窗口管理：`CreateWindowEx`、消息循环（`GetMessage` / `DispatchMessage`）
   - GDI 绘图：`BitBlt`、`StretchBlt`、`CreateCompatibleDC`、字体渲染
   - 颜色与矩形裁剪原语
3. **Linux 平台绑定（`pyx.os.linux`）**
   - X11/xcb：`xcb_connect`、窗口创建、事件循环、`xcb_image_put`
   - Wayland + EGL：`wl_display_connect`、surface、`eglSwapBuffers`
   - Cairo 2D 光栅化（文字、路径、图像合成）
4. **抽象渲染后端接口**
   - 与 rENDER 现有 `backend/` 对齐的平台无关接口
   - `DrawRect` / `DrawText` / `DrawImage` / `DrawBorder` 命令集

### 完成标准
- 在 Windows 和 Linux 上分别能打开原生窗口并绘制矩形 + 文字。

---

## Phase 5：浏览器支撑库（目标：2027 Q1）

### 目标
补全浏览器引擎所需的系统级能力：网络、I/O、并发。

### 计划项
1. **`pyx.io`**：文件读写（UTF-8 / 二进制）、内存映射
2. **`pyx.net`**：TCP socket 封装、HTTP/1.1 客户端、TLS（可选，通过 libssl FFI）
3. **`pyx.thread`**：线程创建、互斥锁、条件变量（浏览器需要并发资源加载）
4. **`pyx.mem`**：Arena 分配器（DOM 树生命周期管理）、手动释放原语
5. **`pyx.codec`**：UTF-8 / Latin-1 解码、Base64、URL 编解码

### 完成标准
- 能并发 HTTP 下载多个 CSS / 图片资源并写入本地缓存。

---

## Phase 6：rENDER 浏览器引擎重写（目标：2027 Q2–Q4）

### 目标
用 PyX 重新实现 rENDER 的完整渲染管线，不依赖 Qt，直接调用原生平台 API。

### 子系统迁移计划（对应 rENDER 模块）

| rENDER 模块 | PyX 对应实现 | 关键依赖 |
|-------------|-------------|----------|
| `html/parser.py`（HTML5 tokenizer） | `browser.html` | `str`、`list[T]`、`class` |
| `html/dom.py`（DOM 树） | `browser.dom` | `class`、Arena 分配器 |
| `css/`（解析、级联、选择器） | `browser.css` | `dict[K,V]`、`str`、`class` |
| `layout/`（block / inline / flex / grid） | `browser.layout` | struct、递归 |
| `js/`（词法、解析、解释器、DOM 绑定） | `browser.js`（后期） | 完整 `class`、闭包子集 |
| `network/`（HTTP 并发加载） | `browser.net` → `pyx.net` | `pyx.thread`、`pyx.net` |
| `rendering/`（显示列表） | `browser.render` | FFI 渲染后端 |
| `backend/`（Qt painter） | `browser.backend.win32` / `.x11` | Phase 4 平台绑定 |

### 功能对齐优先级

与 rENDER roadmap 对齐，按优先级排序：

0. **运行时一致性**：单一渲染路径，拒绝回退和特判
1. **JavaScript 执行**：事件循环、Promise、async 调度、现代语法、模块依赖
2. **DOM 与 Web API**：Mutation API、查询/遍历、事件分发、fetch 行为
3. **自定义元素与 Shadow DOM**：注册、升级时机、shadow root、slot
4. **资源加载**：脚本阻塞、defer 行为、缓存、重试
5. **布局与绘制**：Flex / Grid、替换元素、transform、sticky、伪元素、SVG
6. **兼容性测试**：从真实页面失败案例中提取通用 fixture

### 完成标准
- 能渲染包含 Flex 布局、CSS 级联、图片资源的真实页面，并在 Windows 和 Linux 上以原生窗口展示。

---

## Phase 4（附加）：编译优化与工程化（与 Phase 5/6 并行，目标：2027 Q1）

### 目标
保障浏览器重写期间编译器自身的性能和开发体验。

### 计划项
1. **编译优化**
   - 常量折叠
   - 小函数内联
   - 基础死代码消除
2. **工具链完善**
   - `pyx test` / `pyx run`
   - 构建缓存与增量编译
3. **质量保障**
   - 基准套件（fib / nbody / json / http-handler）
   - CI matrix（Linux / Windows）

### 完成标准
- 数值类基准达到 CPython 5x+，并在关键样例稳定输出可执行产物。

---

## 风险与依赖

1. **语义风险**：Python 语义与静态编译模型存在天然冲突，需要持续明确"可编译子集边界"。
2. **FFI 复杂度**：C ABI 的指针、对齐、调用约定细节多，需要严格测试防止内存安全问题。
3. **平台差异**：Win32 / X11 / Wayland 的事件模型和绘图 API 差异大，抽象层设计需谨慎。
4. **JS 引擎体量**：rENDER 的 JS 引擎约 200KB Python，是最复杂的子系统，可能放到最后或采用嵌入 QuickJS（FFI）的方案。
5. **生态风险**：缺少包管理与跨模块构建能力会限制实际采用，需要尽早补齐。
