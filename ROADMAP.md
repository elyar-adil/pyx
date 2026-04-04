# PyX Roadmap

> Last updated: 2026-04-04

PyX 只负责**纯语言**本身：语法规则、类型系统、编译器、FFI 语言特性、包管理器。
平台绑定（Win32、X11、Wayland 等）、浏览器引擎等属于独立的生态包，不在本仓库职责范围内。

---

## 当前状态（Phase 1 完成，Phase 2 功能完成，验收收尾中）

- ✅ 静态子集检查器（注解约束、变量类型稳定性、反射调用限制）
- ✅ LLVM IR 代码生成（`int` 函数、算术、函数调用、递归、基础 `if`）
- ✅ `pyx build` 构建产物：`.ll` + 可选 `.o`

---

## Phase 2：类型系统与语义补全（功能完成，验收收尾中，目标：2026 Q2）

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

### 已落地（2026-04-04）
- ✅ `while` 基础支持
- ✅ 同类型重赋值（基于 `alloca/load/store`）
- ✅ `bool` / `float` 一等编译支持
- ✅ `if/elif/else` 分支合流补全
- ✅ `int | float` 最小 union 方案（tagged lowering）
- ✅ analyzer / compiler 统一错误格式与错误码
- ✅ 新增 `elif`、union 数值运算、CLI 诊断格式等自动化测试

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
4. **内存管理：ARC + 后台循环收集器**
   - 默认自动引用计数（ARC）：对象离开作用域时编译器插入 inc/dec，用户无感知
   - 后台轻量循环收集器：处理 ARC 无法释放的环形引用（如 DOM 的 `parent` 指针）
   - 逃逸分析（Phase 6 优化）：不逃逸的短生命周期对象直接栈分配，零 malloc
   - 静态分析只用于检测**类型图上显而易见的环**并给出提示，不作强制决策
5. **模块系统**
   - 同包跨文件符号解析与链接
   - `import` 子集（无动态 import）

### 完成标准
- 支持包含多模块、容器操作、自定义类型的示例项目编译。
- 包含环形引用的对象能被正确释放（循环收集器端到端测试）。

---

## Phase 4：C ABI FFI（目标：2026 Q4）

### 目标
让 PyX 能声明并调用任意 C ABI 函数，是生态包（平台绑定、系统库封装等）的基础。

### 设计原则
直接复用标准库 `ctypes` 的 API，**不引入任何新模块或新语法**，采用**动态链接**方式。

`python foo.py` 时 ctypes 正常运行；`pyx build foo.py` 时各语句独立编译：
- `CDLL("c")` → 编译为 `dlopen` 调用，库在运行时加载，编译器无需跨语句解析库归属
- `CFUNCTYPE(...)` → 纯类型信息，不生成运行时代码，结果作为函数指针类型记入 analyzer
- `_puts_t(("puts", _libc))` → 编译为 `dlsym` 调用 + 函数指针类型转换，类型从 `_puts_t` 普通推断即可
- `puts(b"hello")` → 通过函数指针间接调用，签名从 `puts` 的已知类型提取

使用 `CFUNCTYPE` 而非 `argtypes`/`restype` 赋值，原因是后者是 monkey-patch，签名可在任意位置被修改，编译器无法静态确定调用时的签名。

```python
import ctypes

_libc   = ctypes.CDLL("c")                                   # → dlopen("libc.so.6")
_puts_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p)    # → 类型: fn(c_char_p)->c_int
puts    = _puts_t(("puts", _libc))                           # → dlsym(_libc, "puts") + 类型转换

puts(b"hello")                                                # → 函数指针间接调用
```

### 计划项
1. **analyzer 扩展**
   - `ctypes.CFUNCTYPE(restype, *argtypes)` → 记录为函数指针类型，供后续推断使用
   - `CDLL(name)` → 类型标记为"动态库句柄"
   - `fn_t(("symbol", lib))` → 类型从 `fn_t` 推断，生成 `dlopen`/`dlsym` 调用
2. **compiler 扩展**
   - `CDLL` → `dlopen` LLVM IR 调用
   - `fn_t(("sym", lib))` → `dlsym` + `bitcast` 到对应函数指针类型
   - 函数指针调用 → LLVM `call` 通过指针（间接调用）
3. **支持的 ctypes 类型映射**
   - 整数：`c_int` → `i32`，`c_long` → `i64`，`c_size_t` → `i64` 等
   - 浮点：`c_float` → `float`，`c_double` → `double`
   - 指针：`c_void_p` / `c_char_p` → `ptr`，`POINTER(T)` → `ptr`

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
2. **FFI 复杂度**：C ABI 的指针、对齐、调用约定细节多；`CFUNCTYPE` 的运行时行为与 pyx 编译时生成的 LLVM call 必须严格对齐，否则同一份代码在两种环境下结果不同。
3. **内存管理风险**：ARC 的 inc/dec 插入点必须和 Python 引用语义完全一致；循环收集器的触发时机若处理不当会在渲染帧内引入 pause，需要限制其只在帧间或空闲时运行。
4. **实现风险**：手写 LLVM IR 在复杂语义下维护成本高，后续可能需要迁移到结构化 Typed IR。
5. **生态风险**：包管理是生态落地的关键，需要尽早设计包格式和索引协议。
