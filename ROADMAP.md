# PyX Roadmap

> Last updated: 2026-04-02

PyX 只负责**纯语言**本身：语法规则、类型系统、编译器、FFI 语言特性、包管理器。
平台绑定（Win32、X11、Wayland 等）、浏览器引擎等属于独立的生态包，不在本仓库职责范围内。

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
进入"可用于小型真实项目"的阶段。

### 计划项
1. **基础类型扩展**
   - `str` 的原生布局（ptr + len）与基础操作（索引、切片、拼接、UTF-8 编解码）
   - `bytes` 类型
2. **容器类型**
   - `list[T]`：原生布局、`len` / `append` / 索引
   - `dict[K, V]`：哈希表实现
3. **`class` / `dataclass` 支持**
   - 受限 `class` 到 struct 的 lowering
   - 方法调用的静态分派
4. **模块系统**
   - 同包跨文件符号解析与链接
   - `import` 子集（无动态 import）

### 完成标准
- 支持包含多模块、容器操作、自定义类型的示例项目编译。

---

## Phase 4：C ABI FFI（目标：2026 Q4）

### 目标
让 PyX 能声明并调用任意 C ABI 函数，是生态包（平台绑定、系统库封装等）的基础。

### 设计原则
直接复用标准库 `ctypes` 的 API，**不引入任何新模块或新语法**。
`python foo.py` 时 ctypes 正常运行；`pyx build foo.py` 时编译器静态识别 ctypes 惯用模式，生成原生 LLVM extern 调用，零 ctypes 运行时开销。

```python
import ctypes

libc = ctypes.CDLL("c")           # 编译时：链接 -lc

libc.puts.argtypes = [ctypes.c_char_p]
libc.puts.restype  = ctypes.c_int  # 编译时：提取函数签名

libc.puts(b"hello")               # 编译时：生成原生 call 指令
```

### 计划项
1. **编译器模式识别**
   - analyzer 识别 `ctypes.CDLL("name")` → 记录链接依赖 `-lname`
   - analyzer 识别模块级 `lib.fn.argtypes` / `lib.fn.restype` 赋值 → 提取函数签名
   - compiler 将 `lib.fn(args)` 调用编译为 LLVM IR extern 声明 + call 指令
2. **支持的 ctypes 类型**
   - 整数：`c_int`、`c_long`、`c_longlong`、`c_size_t` 等
   - 浮点：`c_float`、`c_double`
   - 指针：`c_void_p`、`c_char_p`、`POINTER(T)`
3. **构建集成**
   - `pyx build` 根据识别到的 `CDLL` 自动向链接器传递 `-l` 参数
   - `build_report.json` 记录外部库依赖

### 完成标准
- 同一份 `.py` 文件既能被 `python` 直接运行，也能被 `pyx build` 编译为原生可执行文件。
- 能通过标准 ctypes 写法调用 libc 函数（如 `puts`、`abs`）并通过端到端测试。

---

## Phase 5：包管理器（目标：2027 Q1）

### 目标
提供类似 pip 的包管理能力，让平台绑定、GUI 库、网络库等作为独立包分发。

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
追求"可发布"的性能和开发体验。

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
2. **FFI 复杂度**：C ABI 的指针、对齐、调用约定细节多；`pyx.ffi` 的运行时（ctypes）与编译时行为必须严格对齐，否则同一份代码在两种环境下结果不同。
3. **实现风险**：手写 LLVM IR 在复杂语义下维护成本高，后续可能需要迁移到结构化 Typed IR。
4. **生态风险**：包管理是生态落地的关键，需要尽早设计包格式和索引协议。
