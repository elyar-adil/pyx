# PyX Prototype

PyX（Python eXtended Static）是一个"Python 严格子集 + 类型驱动编译"原型，目标是编译为原生可执行文件。

## 当前实现

- `pyx check <file.py>`：对源文件执行静态子集检查。
- `pyx build <file.py> -o dist/`：先检查，再编译成 LLVM IR（`.ll`），并在本机有 `clang` 时继续产出原生目标文件（`.o`）。

## 已实现的子集规则

1. 函数参数与返回值必须显式注解。
2. 变量首次赋值后类型不可变更。
3. 禁止所有 monkey-patch：`getattr` / `setattr` / `delattr` 一律禁止；class 体外对实例添加未声明字段（如 `a.z = 1`，其中 `z` 未在 class 中声明）也是编译错误。所有字段必须在 class 体内以类型注解声明，编译器据此确定 struct 的固定内存布局，`obj.field = val`（field 已声明）编译为单条 `store` 指令。
4. 容器字面量会推导为 `list[T]` / `dict[K,V]` / `set[T]`，空容器推导为 `list[Any]` 等宽松类型。

## LLVM 编译子集（当前支持）

- 顶层函数（`def`）
- `int` / `float` / `bool` 参数与返回值
- 表达式：整数/浮点/布尔字面量，`+`、`-`、`*`，函数调用（含递归）
- 控制流：`if/else` 与 `while`（比较支持 `< <= > >= ==`）

## 用法

```bash
python -m pyx.cli check examples/fib.py
python -m pyx.cli build examples/fib.py -o dist
```

构建目录中会包含：

- `<name>.ll`：LLVM IR
- `<name>.o`：原生目标文件（如果系统可用 `clang`）
- `build_report.json`：构建元数据

## 原生 API 调用

PyX 在 Phase 4 支持以标准 `ctypes` 写法调用原生系统库，编译时生成原生调用指令，不引入任何新语法：

```python
import ctypes

_libc   = ctypes.CDLL("c")                                  # → dlopen at runtime
_puts_t = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p)   # → 函数指针类型
puts    = _puts_t(("puts", _libc))                          # → dlsym + 类型转换

puts(b"hello")   # python foo.py 和 pyx build foo.py 均可运行
```

## 生态系统

PyX 本身只负责语言核心；平台绑定（Win32、X11、Wayland 等）、网络库、GUI 框架等由独立的生态包提供，基于 ctypes 模式封装，通过 `pyx pkg` 包管理器安装。

## Roadmap

详见 [`ROADMAP.md`](./ROADMAP.md)。
