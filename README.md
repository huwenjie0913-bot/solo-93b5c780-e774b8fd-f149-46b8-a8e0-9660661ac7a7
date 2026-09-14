# 自动移液程序审查 API

FastAPI 接口按执行顺序模拟微孔板液体、枪头残留和污染传播，支持版本比较和最少干预点求解。

## 启动

```bash
pip install -r requirements.txt
uvicorn app.main:app --reload
```

## 接口

- `POST /api/audit`：审查单个程序，可附带目标孔污染阈值。
- `POST /api/programs/audit`：直接提交 `ProgramInput`。
- `POST /api/programs/compare`：比较 A/B 两版程序，返回阈值判定、版本间改善、总体结论，并为 B 版求最少换头/洗针点。
- `GET /health`：健康检查。

## 步骤类型

- `aspirate`：从 `source` 吸入 `volume`。
- `dispense`：向 `target` 排出 `volume`。
- `mix`：在 `well` 内吸排 `cycles` 次。
- `wash`：按 `wash_effectiveness` 等比例降低枪头内各污染包体积；`1.0` 等价于完全清空。
- `change_tip`：切换到新枪头。

`residual_rate` 可放在程序全局、枪头策略默认值或步骤级，步骤级优先。

## 最少干预点的计算依据

求解器为每个“吸入/混匀前枪头边界”建立单位容量顶点，其余液体转移为无限容量边，将所有首次到达目标孔的污染节点连接到超级汇点。由最大流-最小割定理，单位容量顶点割的大小就是阻断全部传播路径所需的最少换头或完全洗针次数；随后会重新模拟割点集合并验证阈值。

阈值允许少量污染时，求解器还会在候选数量较小的情况下穷举更少干预组合；否则返回严格阻断所有传播路径的最小割解。

## 回归测试

环境缺少 FastAPI/Pydantic 时，测试会注入最小桩以直接执行核心和端点函数：

```bash
python tests/test_regression.py
```

安装依赖后也可用：

```bash
pytest -q
```
