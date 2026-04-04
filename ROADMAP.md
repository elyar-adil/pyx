# PyX Roadmap

> Last updated: 2026-04-05

PyX 只负责**纯语言**本身：语法规则、类型系统、编译器、FFI 语言特性、包管理器。
平台绑定（Win32、X11、Wayland 等）、浏览器引擎等属于独立的生态包，不在本仓库职责范围内。

---

## 当前状态（Phase 2 已完成，Phase 3 核心已落地，Phase 4 基础能力已落地）

- ✅ 静态子集检查器：注解约束、类型稳定性、反射限制、模块感知
- ✅ LLVM IR 代码生成：`int` / `float` / `bool` / `str` / `list[T]` / `class` struct
- ✅ 多模块项目：`import` + `from ... import`，跨文件符号解析与链接
- ✅ `pyx build` 构建产物：`.ll` + 可选 `.o`
- ✅ Phase 4 基础 FFI：`ctypes.CDLL`、`ctypes.CFUNCTYPE`、`dlsym` 绑定、函数指针间接调用
- ✅ 68 个自动化测试全部通过

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

## Phase 3：数据结构与模块系统（🚧 进行中，核心能力已落地）

### 目标
进入“可用于小型真实项目”的阶段。

### 已落地（2026-04-05，68 个测试全通过）

**类型与表达式**
- ✅ `str` 原生布局（`{ ptr, i64 }`）：字面量、变量、参数、`print`、`len()`、`+` 拼接（`malloc + memcpy`）、`s[i]` 索引（UTF-8 helper）
- ✅ `list[T]` 原生布局（`{ ptr, i64, i64 }`）：字面量、`len()`、`append()`（`realloc` 扩容）、`xs[i]` 索引、`xs[i] = v`
- ✅ `class` / `dataclass` -> fixed-layout struct：字段读写（`extractvalue` / `insertvalue`）、方法调用静态分派（`self` 作为第一参数）
- ✅ `from mod import Cls` 构造函数调用

**模块系统**
- ✅ `import mod` 和 `from mod import sym`（函数 + 类）
- ✅ 跨模块 analyzer：符号解析、类型推断、方法签名验证
- ✅ 跨模块 compiler：函数符号 mangling（`@mod_X__fn`）、类类型 mangling（`%type.X__Cls`）

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
| `bytes` LLVM lowering | ❌ | Analyzer 已识别；Compiler 明确拦截 |
| 除法（`/`, `//`, `%`） | ❌ | 需要 `sdiv` / `fdiv` / `srem`，以及除零语义设计 |
| `break` / `continue` | ❌ | 需要维护循环跳转标签栈 |
| ARC 内存管理 | ❌ | 当前 `str` 拼接、`list` 扩容等 heap 分配尚未释放 |
| 循环引用收集器 | ❌ | 依赖 ARC 先落地 |
| 逃逸分析 | ❌ | 归入 Phase 6 优化项 |

### 完成标准
- 支持包含多模块、`list[T]` / `class` / `str` 操作的完整示例项目编译并运行。
- `dict[K, V]` 基础操作（set / get）端到端通过。
- 为 heap 值建立明确的所有权/释放策略，不再长期依赖“只分配不释放”。

---

## Phase 4：C ABI FFI（🚧 基础能力已落地，继续扩展）

### 目标
让 PyX 能以标准 `ctypes` 写法声明并调用 C ABI 函数，为生态包（平台绑定、系统库封装等）提供语言级基础。

### 已落地（2026-04-05）
- ✅ `import ctypes` 与 `from ctypes import ...` 两种导入形式都可被 analyzer / compiler 识别
- ✅ `ctypes.CDLL("libc.so.6")` 降级为 `dlopen`，内部类型标记为动态库句柄
- ✅ `ctypes.CFUNCTYPE(restype, *argtypes)` 记录为函数指针类型，不生成多余运行时代码
- ✅ `fn_t(("symbol", lib))` 降级为 `dlsym + bitcast`
- ✅ 对绑定后的函数指针执行 LLVM 间接调用（`call <ret> (<args>) ...`）
- ✅ 已覆盖 `c_int`、`c_long`、`c_float`、`c_double`、`c_size_t`、`c_void_p`、`c_char_p`、`c_wchar_p`
- ✅ 25 个 Phase 4 测试通过（13 analyzer + 12 compiler）

### 当前边界
- `CFUNCTYPE` 路径是**静态可分析**的主路径；`argtypes` / `restype` monkey-patch 方案继续明确不支持。
- 指针类 `ctypes` 返回值当前按 opaque 值处理，映射回 PyX 侧为 `Any`，还不具备丰富的静态语义。
- 当前实现直接面向 `dlopen` / `dlsym`，属于 POSIX-first 路径，不等于已完成跨平台装载层。

### 剩余工作

| 特性 | 状态 | 说明 |
|------|------|------|
| 真实端到端运行时验证 | 🚧 | 当前测试重点是 analyzer / LLVM IR lowering；还缺“同一份代码直接运行与编译运行”的回归样例 |
| `POINTER(T)` / 更完整指针类型系统 | ❌ | 当前类型解析尚未覆盖 `POINTER(T)` 这类组合形式 |
| `bytes` / `c_char_p` 所有权与互操作 | ❌ | 需要和 `bytes` lowering、内存管理策略一起设计 |
| 跨平台动态装载抽象 | ❌ | Windows 下需要对应 `LoadLibrary` / `GetProcAddress` 路径 |
| 更宽 ctypes API 覆盖 | ❌ | 目前只覆盖对静态编译最关键的 `CDLL` + `CFUNCTYPE` + 绑定调用链 |

### 完成标准
- 同一份 `.py` 文件既能被 `python` 直接运行，也能被 `pyx build` 编译后的产物稳定执行。
- 能通过标准 `ctypes` 写法调用 libc / libm 函数（如 `puts`、`abs`、`sqrt`）并具备运行时回归测试。
- 为指针/字符串参数建立明确且可文档化的 ABI 约束。

---

## Phase 5：包管理器（目标：2027 Q1）

### 目标
提供类似 `pip` 的包管理能力，让平台绑定、GUI 库、网络库等作为独立包分发。

### 计划项
1. **`pyx pkg` CLI**
   - `pyx pkg install <name>`：从中央索引下载并安装包
   - `pyx pkg publish`：打包并发布到索引
2. **包格式**
   - `pyx.toml`：包元数据（名称、版本、依赖、动态库声明）
   - 支持预编译的 `.o` / `.a` 分发（跨平台 FFI 包的常见形式）
3. **依赖解析**
   - 语义化版本约束
   - lock 文件（`pyx.lock`）

### 完成标准
- 能发布一个封装 libc 的最小包，并在另一个项目中通过 `pyx pkg install` 使用它。

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
