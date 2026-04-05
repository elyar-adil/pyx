# PyX Roadmap

> Last updated: 2026-04-05（Phase 5 包管理器）

PyX 只负责**纯语言**本身：语法规则、类型系统、编译器、FFI 语言特性、包管理器。
平台绑定（Win32、X11、Wayland 等）、浏览器引擎等属于独立的生态包，不在本仓库职责范围内。

---

## 当前状态（Phase 2 已完成，Phase 3 核心与基础运行时已落地，Phase 4 扩展能力已落地，Phase 5 包管理器已落地）

- ✅ 静态子集检查器：注解约束、类型稳定性、反射限制、模块感知
- ✅ LLVM IR 代码生成：`int` / `float` / `bool` / `str` / `bytes` / `list[T]` / `class` struct
- ✅ 多模块项目：`import` + `from ... import`，跨文件符号解析与链接
- ✅ 基础文件 I/O：`open()`、`read()`、`write()`、`readline()`、`close()`、`with open(...) as`
- ✅ `pyx build` 构建产物：`.ll` + 可选 `.o`
- ✅ Phase 4 扩展 FFI：`POINTER(T)` 组合指针类型、`str`/`bytes` ↔ `c_char_p` 互操作、`c_char_p` 返回 → `bytes`（strlen 路径）、`Any` 不透明指针（`c_void_p`）、`ctypes.string_at(ptr, size)` → `bytes`、cfuncptr 调用参数类型检查
- ✅ Phase 5 包管理器：`pyx.toml` 格式、语义化版本约束、本地注册表、`pyx.lock` 锁文件、`pyx pkg install` / `pyx pkg publish`、`pyx_packages/` 搜索路径
- ✅ 162 个自动化测试全部通过

---

## Phase 2：类型系统与语义补全（✅ 已完成，2026 Q2）

### 已落地
- ✅ `if/elif/else` 分支合流（phi 节点）
- ✅ `while` 循环
- ✅ 同类型重赋值（`alloca/load/store`）
- ✅ `bool` / `float` 一等编译支持
- ✅ `int | float` union（tagged lowering）
- ✅ 统一 analyzer / compiler 错误格式与错误码（`PYX1xxx` / `PYX2xxx`）
- ✅ `!=` 比较运算符（int、float、bool、union 全路径）
- ✅ 一元运算符：`not`（xor i1 1）、`-`（sub 0 / fneg）
- ✅ `x: T = expr` 带注解赋值（AnnAssign）与 analyzer 完全对齐

### 已知不在 Phase 2 范围
- 除法（`/`, `//`, `%`）、`break` / `continue`：留到 Phase 3/后续补齐

---

## Phase 3：数据结构、模块系统与基础运行时（🚧 进行中，核心能力已落地）

### 目标
进入“可用于小型真实项目”的阶段。

### 已落地（2026-04-05，总测试数 93；其中新增 25 个文件 I/O / bytes 回归）

**类型与表达式**
- ✅ `str` 原生布局（`{ ptr, i64 }`）：字面量、变量、参数、`print`、`len()`、`+` 拼接（`malloc + memcpy`）、`s[i]` 索引（UTF-8 helper）
- ✅ `bytes` 原生布局（`{ ptr, i64 }`）：字面量、`len()`、二进制文件读写返回值
- ✅ `list[T]` 原生布局（`{ ptr, i64, i64 }`）：字面量、`len()`、`append()`（`realloc` 扩容）、`xs[i]` 索引、`xs[i] = v`
- ✅ `class` / `dataclass` -> fixed-layout struct：字段读写（`extractvalue` / `insertvalue`）、方法调用静态分派（`self` 作为第一参数）
- ✅ `from mod import Cls` 构造函数调用

**模块系统**
- ✅ `import mod` 和 `from mod import sym`（函数 + 类）
- ✅ 跨模块 analyzer：符号解析、类型推断、方法签名验证
- ✅ 跨模块 compiler：函数符号 mangling（`@mod_X__fn`）、类类型 mangling（`%type.X__Cls`）

**基础运行时 / I/O**
- ✅ `open(name, mode)` 区分 `TextFile` / `BinaryFile`
- ✅ 文本文件：`read()` / `readline()` / `write()` / `close()`
- ✅ 二进制文件：`read()` / `write()` / `close()`
- ✅ `with open(...) as fp:` 上下文管理器 lowering（单项 `with`）

**正确性与工程行为**
- ✅ `_compile_list_literal` 消除双重编译（避免重复 IR 指令）
- ✅ `llvm_type()` 对未知类型抛出 `CompileError`，不再泄漏裸 `KeyError`
- ✅ Analyzer `ast.Expr` 语句检查：只允许函数调用，非调用表达式明确报错（`PYX1014`）
- ✅ analyzer 已识别但 LLVM 尚未降级的类型，统一报出清晰的 `planned but not lowered` 错误

### 尚未实现（Phase 3 剩余工作）

| 特性 | 状态 | 说明 |
|------|------|------|
| `str[i:j]` 切片 | ❌ | 需要 `malloc + memcpy`；索引路径已具备基础 |
| `str` 比较（`==`, `!=`） | ❌ | 当前会显式报“planned but not lowered”，尚未接入 `memcmp` |
| `dict[K, V]` LLVM lowering | ❌ | Analyzer 已支持类型推断；Compiler 尚未实现哈希表布局与操作 |
| `set[T]` LLVM lowering | ❌ | Analyzer 可推断类型；Compiler 明确拦截 |
| `bytes` 扩展操作（索引 / 切片 / 比较） | ❌ | 当前只覆盖字面量、`len()` 与二进制文件 I/O 主路径 |
| 更完整文件 API（`seek` / `tell` / 迭代 / 追加语义） | ❌ | 当前只覆盖 `read` / `readline` / `write` / `close` |
| 动态 `open()` mode lowering | ❌ | compiler 当前要求 mode 为字符串字面量 |
| 除法（`/`, `//`, `%`） | ❌ | 需要 `sdiv` / `fdiv` / `srem`，以及除零语义设计 |
| `break` / `continue` | ❌ | 需要维护循环跳转标签栈 |
| ARC 内存管理 | ❌ | 当前 `str` 拼接、`list` 扩容等 heap 分配尚未释放 |
| 循环引用收集器 | ❌ | 依赖 ARC 先落地 |
| 逃逸分析 | ❌ | 归入 Phase 6 优化项 |

### 完成标准
- 支持包含多模块、`list[T]` / `class` / `str` 操作的完整示例项目编译并运行。
- `dict[K, V]` 基础操作（set / get）端到端通过。
- 为 heap 值建立明确的所有权/释放策略，不再长期依赖“只分配不释放”。
- 文件 I/O 从“可用”推进到“可写真实脚本”的完整运行时子集。

---

## Phase 4：C ABI FFI（🚧 扩展能力已落地，继续完善）

### 目标
让 PyX 能以标准 `ctypes` 写法声明并调用 C ABI 函数，为生态包（平台绑定、系统库封装等）提供语言级基础。

### 已落地（2026-04-05 第二轮扩展，共 40 个 Phase 4 测试）
- ✅ `import ctypes` 与 `from ctypes import ...` 两种导入形式都可被 analyzer / compiler 识别
- ✅ `ctypes.CDLL(“libc.so.6”)` 降级为 `dlopen`，内部类型标记为动态库句柄
- ✅ `ctypes.CFUNCTYPE(restype, *argtypes)` 记录为函数指针类型，不生成多余运行时代码
- ✅ `fn_t((“symbol”, lib))` 降级为 `dlsym + bitcast`
- ✅ 对绑定后的函数指针执行 LLVM 间接调用（`call <ret> (<args>) ...`）
- ✅ 已覆盖 `c_int`、`c_long`、`c_float`、`c_double`、`c_size_t`、`c_void_p`、`c_char_p`、`c_wchar_p` 等全部基础 ctypes 类型
- ✅ **`POINTER(T)` 组合指针类型**：analyzer / compiler 均识别 `ctypes.POINTER(T)` 并降级为 `ptr`（等同于 `c_void_p`）；可用于 CFUNCTYPE 签名中
- ✅ **`str` / `bytes` → `c_char_p` 参数传递**：编译器从 `%pyx.str` / `%pyx.bytes` struct 中 `extractvalue` 出数据指针，analyzer 做类型兼容性检查
- ✅ **`c_char_p` 返回值 → `bytes`**：通过 `strlen` 获取 C 字符串长度，malloc + memcpy 拷贝到独立堆缓冲区，包装成 `%pyx.bytes`
- ✅ **`Any` 不透明指针**（`c_void_p` 等返回）：`Any` 类型现在可合法分配槽（LLVM 类型 `ptr`），`is_supported_type(“Any”)` 返回 True
- ✅ **`ctypes.string_at(ptr, size) → bytes`**：从裸 C 指针按指定长度拷贝数据，返回 `%pyx.bytes`
- ✅ **cfuncptr 调用参数类型检查**：analyzer 现在在间接调用时检查 PyX 类型与 ctypes 期望类型的兼容性（int / float / str / bytes / Any 分情况）

### 当前边界
- `CFUNCTYPE` 路径是**静态可分析**的主路径；`argtypes` / `restype` monkey-patch 方案继续明确不支持。
- `c_char_p` → `bytes` 路径会做堆拷贝（malloc + memcpy），不保留 C 侧指针所有权；调用方不应在 C 函数内部释放该指针。
- `bytes` → `c_char_p` 参数只传递数据指针，不自动追加 `\0`；需要传递 null-terminated 内容时调用方应自行在 bytes 末尾包含 `\0`（如 `b”hello\0”`）。
- `POINTER(T)` 目前统一降级为 `ptr`（不跟踪 T 的静态类型信息）；未支持解引用或复合指针运算。
- 当前实现直接面向 `dlopen` / `dlsym`，属于 POSIX-first 路径，不等于已完成跨平台装载层。

### 剩余工作

| 特性 | 状态 | 说明 |
|------|------|------|
| 真实端到端运行时验证 | 🚧 | 测试覆盖 analyzer / LLVM IR；还缺”同一份 .py 直接运行与编译产物执行结果对比”的 regression |
| `bytes` null-terminated 场景的便捷封装 | ❌ | 当前需要用户手动在 bytes 末尾包含 `\0`；可考虑提供 `ctypes.c_str(s: str)` 辅助 |
| `POINTER(T)` 类型信息保留 | ❌ | 当前统一降级为 `ptr`；未来可考虑在 cfuncptr 类型字符串中保留 `ptr(T)` 信息用于更严格检查 |
| `byref(obj)` / `addressof(obj)` | ❌ | 需要堆分配 + 指针语义；依赖 ARC / 所有权模型先落地 |
| `ctypes.cast(val, type)` | ❌ | 指针重解释；在所有指针统一为 `ptr` 后实现较简单，但需要 analyzer 追踪目标类型 |
| 跨平台动态装载抽象 | ❌ | Windows 下需要对应 `LoadLibrary` / `GetProcAddress` 路径 |
| Struct / Union 类型 (`ctypes.Structure`) | ❌ | C 结构体直接映射到 PyX class 的 FFI 路径；属于较大工作量 |

### 完成标准
- 同一份 `.py` 文件既能被 `python` 直接运行，也能被 `pyx build` 编译后的产物稳定执行（有 runtime regression 覆盖）。
- 能通过标准 `ctypes` 写法调用 libc / libm 函数（如 `puts`、`abs`、`sqrt`、`getenv`）并具备运行时回归测试。
- 为指针/字符串参数建立明确且可文档化的 ABI 约束（null-termination 要求、所有权语义等）。

---

## Phase 5：包管理器（✅ 已完成，2026 Q2）

### 目标
提供类似 `pip` 的包管理能力，让平台绑定、GUI 库、网络库等作为独立包分发。

### 已落地（2026-04-05，新增 54 个 Phase 5 测试，总测试数 162）

**`pyx pkg` CLI**
- ✅ `pyx pkg install <name>`：从本地注册表下载并安装包到 `pyx_packages/`
- ✅ `pyx pkg publish`：打包当前目录并发布到注册表（`--registry` / `PYX_REGISTRY` 环境变量可覆盖路径）

**包格式（`pyx.toml`）**
- ✅ `[package]` 节：`name`、`version`（严格 semver x.y.z）、`description`
- ✅ `[dependencies]` 节：`dep-name = "约束字符串"` 格式
- ✅ `[libraries]` 节：动态库声明（`alias = { path = "libc.so.6" }`）
- ✅ `save_manifest()` 序列化为合法 TOML

**语义化版本（`pyx.pkg.semver`）**
- ✅ `Version` 数据类：解析 `x.y.z`，全序比较
- ✅ 单个约束运算符：`==`、`!=`、`>=`、`>`、`<=`、`<`、`^`（兼容发布）、`~`（近似等价）
- ✅ 逗号分隔复合约束（AND 语义，如 `>=1.0.0,<2.0.0`）
- ✅ `best_matching(versions, constraint)` 返回满足约束的最高版本

**本地注册表（`pyx.pkg.registry`）**
- ✅ 目录结构：`index.json` + `packages/<name>-<version>.tar.gz`
- ✅ 默认路径 `~/.pyx/registry`，可通过 `PYX_REGISTRY` 环境变量覆盖
- ✅ `publish(archive, name, version)` 计算 sha256 并写入索引
- ✅ `list_versions(name)` / `get_checksum(name, version)` / `fetch_archive(name, version)`

**依赖解析与锁文件（`pyx.pkg.resolver`）**
- ✅ `resolve_dependencies(manifest, registry)` 解析所有直接依赖并选取最高满足版本
- ✅ `LockFile` / `LockedPackage`：JSON 格式 `pyx.lock`，`load` / `save` / `find`

**安装器（`pyx.pkg.installer`）**
- ✅ `install_package(name, registry, install_dir, constraint)` 下载并解压到 `pyx_packages/<name>/`，校验 sha256
- ✅ `publish_package(source_dir, manifest, registry)` 打包为 `.tar.gz`（自动排除 `__pycache__`、`dist`、`.git`、`pyx.lock`）
- ✅ `install_from_manifest(manifest, registry, project_dir)` 批量安装依赖并写入 `pyx.lock`

**模块解析扩展（`project.py`）**
- ✅ `_resolve_module_path` 新增 `pyx_packages/` 搜索路径：先找 `pyx_packages/<name>/<name>.py`，再找 `pyx_packages/<name>.py`

### 完成标准验证
- ✅ 能发布封装 libc 的最小包（`libc-wrap`），并在另一个项目中通过 `install_from_manifest` 安装并通过 `load_project` 正确解析其模块。

### 当前边界
- 注册表为本地目录（POSIX-first）；远端 HTTP 索引与认证留到后续扩展。
- 目前只支持直接依赖解析；传递依赖（从已发布包的 `pyx.toml` 中读取）尚未实现（需要在注册表中存储元数据）。
- 预编译 `.o` / `.a` 分发格式（Phase 5 计划项 2）已通过 tarball 机制支持，但构建系统尚未自动链接安装包中的 `.o` 文件。

---

## Phase 6：优化与工程化（目标：2027 Q2）

### 目标
追求“可发布”的性能和开发体验。

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

1. **语义风险**：Python 语义与静态编译模型存在天然冲突，需要持续明确“可编译子集边界”。
2. **FFI 复杂度**：C ABI 的指针、对齐、调用约定细节很多；`CFUNCTYPE` 的 Python 运行时行为必须与 PyX 生成的 LLVM call 保持一致。
3. **内存管理风险**：当前已经有真实 heap 值（`str` / `list` / FFI 互操作）进入主路径，若 ARC/所有权模型设计不清，后续会持续放大实现复杂度。
4. **实现风险**：手写 LLVM IR 在复杂语义下维护成本较高，后续可能需要迁移到结构化 Typed IR。
5. **生态风险**：包管理仍是生态落地关键，需要尽早稳定包格式和索引协议。
