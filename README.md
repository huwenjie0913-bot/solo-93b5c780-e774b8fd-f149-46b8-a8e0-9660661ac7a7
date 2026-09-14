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

## 来源级污染规则

当两个样本含有同名组分（例如都叫 `DNA`）时，仅按组分名判断无法识别 A 孔样本经枪头残留进入 B 孔样本的串样。来源级规则按“初始液体来源”记账：

- 在 `initial_liquids` 上声明可选的 `sample_id` / `lot_id`；来源身份随每个液体包传播，不被混匀或同名组分合并。
- 在 `source_targets` 中为目标孔配置：
  - `allowed_sources`：允许来源白名单，每条为 `{sample_id, lot_id}`，只填一个时按该字段匹配；空规则 `{}` 是匹配任意来源的通配符；省略整个列表时仅允许该孔初始液体自身声明的来源。
  - `max_foreign_fraction`：非允许来源液体占孔内总体积的最大比例，默认 `0.0`。
- 模拟过程按来源保留体积分账，在初始状态与每一步之后评估：
  - 峰值与最终外来来源占比；
  - 首次越界的步骤（初始即违规时 `step_id` 为 `null` 且 `blockable=false`，求解器返回 `infeasible`）；
  - 来源构成（允许 / 外来 / 未声明体积，含组分细分）；
  - 每个外来来源的完整传播链（吸入、排液残留、混匀携带、混匀回流等）。

未提供 `sample_id` / `lot_id` 的液体视为“未声明背景体积”：计入分母但永不判为外来；未配置 `source_targets` 的旧请求输出与历史行为完全一致。

## 最少干预点的计算依据

求解器为每个“吸入/混匀前枪头边界”建立单位容量顶点，其余液体转移为无限容量边，将所有首次到达目标孔的污染节点（组分级与来源级）连接到超级汇点。由最大流-最小割定理，单位容量顶点割的大小就是阻断全部传播路径所需的最少换头或完全洗针次数；随后会重新模拟割点集合并验证阈值。若目标孔初始液体本身就含有非允许来源，则不存在中途可阻断的传播路径，直接返回 `infeasible`。

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
