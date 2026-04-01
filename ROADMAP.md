# PyX Roadmap

> Last updated: 2026-04-01

## 当前状态（Phase 1 完成，Phase 2 进行中）

- ✅ 静态子集检查器（注解约束、变量类型稳定性、反射调用限制）
- ✅ LLVM IR 代码生成（`int` 函数、算术、函数调用、递归、基础 `if`）
- ✅ `pyx build` 构建产物：`.ll` + 可选 `.o`
- ✅ 模块级语句编译：`if __name__ == "__main__"` 及全局表达式/赋值 → C `main` 入口点

---

## Phase 2：类型系统与语义补全（进行中，目标：2026 Q2）

### 目标
- 扩展可编译语法覆盖，减少“能检查不能编译”的差距。

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
- ✅ 模块级语句编译进 C `main` 入口点
  - `if __name__ == "__main__"` 编译期折叠为 `True`，body 无条件内联
  - 支持模块级带参函数调用、多条语句、`x = expr`、`x: T = expr`
  - 存在同名 PyX `main` 函数时自动重命名为 `pyx_main` 避免符号冲突
- ✅ `ast.AnnAssign`（`x: int = 1`）在函数体与模块级均支持

### 完成标准
- 至少 20 个端到端用例通过（check + build）。

---

## Phase 3：数据结构与 Python 互操作（目标：2026 Q3）

### 目标
- 进入“可用于小型真实项目”的阶段。

### 计划项
1. `list[int]` 的原生布局与常用操作（len/index）
2. 受限 `class` / `dataclass` 到 struct 的 lowering
3. Python runtime fallback 边界（动态区块桥接）
4. 模块级构建（跨文件符号解析）

### 完成标准
- 支持一个包含多模块 + 容器操作的示例项目编译。

---

## Phase 4：优化与工程化（目标：2026 Q4）

### 目标
- 追求“可发布”的性能和开发体验。

### 计划项
1. 编译优化
   - 常量折叠
   - 内联（小函数）
   - 基础死代码消除
2. 工具链完善
   - `pyx test` / `pyx run`
   - 构建缓存与增量编译
3. 质量保障
   - 基准套件（fib/nbody/json/web-handler）
   - CI matrix（Linux/macOS）

### 完成标准
- 数值类基准达到 CPython 5x+，并在关键样例稳定输出可执行产物。

---

## 风险与依赖

1. **语义风险**：Python 语义与静态编译模型存在天然冲突，需要持续明确“可编译子集边界”。
2. **实现风险**：手写 LLVM IR 在复杂语义下维护成本高，后续可能需要迁移到结构化 Typed IR。
3. **生态风险**：缺少包管理与跨模块构建能力会限制实际采用，需要尽早补齐。

