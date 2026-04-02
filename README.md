# PyX Prototype

PyX（Python eXtended Static）是一个"Python 严格子集 + 类型驱动编译"原型，目标是编译为原生可执行文件并能直接调用平台原生 API。

**长期目标**：用 PyX 重写 [rENDER](https://github.com/elyar-adil/rENDER) 浏览器引擎，以 Win32/GDI 和 X11/Cairo 替代 Qt，验证 PyX 可用于实现生产级原生 GUI 应用。

## 当前实现

- `pyx check <file.py>`：对源文件执行静态子集检查。
- `pyx build <file.py> -o dist/`：先检查，再编译成 LLVM IR（`.ll`），并在本机有 `clang` 时继续产出原生目标文件（`.o`）。

## 已实现的子集规则

1. 函数参数与返回值必须显式注解。
2. 变量首次赋值后类型不可变更。
3. 禁止 `getattr` / `setattr` / `delattr` 这类依赖反射的路径。
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

## 原生 API 支持计划

PyX 将在 Phase 4 引入 C ABI FFI，支持直接声明并调用原生系统库：

```python
# 示意语法（Phase 4 目标）
#link "user32"
extern "C" def CreateWindowExA(
    dwExStyle: int,
    lpClassName: ptr[str],
    lpWindowName: ptr[str],
    dwStyle: int,
    x: int, y: int, nWidth: int, nHeight: int,
    hWndParent: void_ptr,
    hMenu: void_ptr,
    hInstance: void_ptr,
    lpParam: void_ptr,
) -> void_ptr: ...
```

目标平台：
- **Windows**：Win32 / GDI（窗口、消息循环、GDI 绘图）
- **Linux**：X11/xcb 或 Wayland+EGL + Cairo 光栅化

## Roadmap

详见 [`ROADMAP.md`](./ROADMAP.md)。
