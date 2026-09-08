---
title: 训练参数说明
category: 训练
order: 3
summary: SFT / 偏好对齐 / 强化学习的关键超参与经验值
---

# 训练参数说明

## 算法与框架对照

| 算法 | 框架 | 数据格式 | 何时用 |
|---|---|---|---|
| SFT | LLaMA-Factory | `sft_alpaca` / `sft_sharegpt` | 所有后训练的起点 |
| DPO | LLaMA-Factory / TRL | `dpo_pairs` | 有成对偏好数据 |
| ORPO | LLaMA-Factory | `dpo_pairs` | 想省掉参考模型、一步到位 |
| SimPO | LLaMA-Factory | `dpo_pairs` | 长度归一化，无参考模型 |
| KTO | LLaMA-Factory / TRL | `kto_pairs` | 只有点赞/点踩这类二元反馈 |
| GRPO | verl / TRL | `verl_parquet` / `harbor_tasks` | 有可自动打分的任务 |
| DAPO | verl | `verl_parquet` | 长思维链 RL，GRPO 不稳时 |
| GSPO | TRL | `verl_parquet` / `harbor_tasks` | MoE / 大模型，GRPO 方差大时 |
| PPO / RLOO / REINFORCE++ / ReMax | verl | `verl_parquet` | 特定实验需求 |

**框架怎么选**：数据是 parquet、要多机或 Megatron → verl；要和 HF 生态一致、或要拿
**Harbor 任务树做 agentic RL** → TRL。选了 Harbor 任务树时平台会自动切到 TRL。

---

## SFT / 偏好对齐

| 参数 | 经验值 | 说明 |
|---|---|---|
| 微调方式 | 迭代期 LoRA，收尾再考虑全参 | LoRA 显存友好；全参需多卡 + ZeRO-3 |
| LoRA rank / alpha | rank 8 起步，难任务 32/64；alpha ≈ 2×rank | 容量不够就调 rank |
| 学习率 | LoRA `1e-4`；全参 `1e-5`；偏好对齐 `5e-6` | 偏好对齐学习率显著低于 SFT |
| cutoff_len | 覆盖 95% 样本即可 | 越长显存越吃紧 |
| batch × 梯度累积 | 等效 batch 32~128 | 等效 = 单卡 batch × 累积 × 卡数 |
| DeepSpeed | 多卡全参用 ZeRO-3 | LoRA 一般不需要 |
| pref_beta | DPO `0.1`；SimPO `2.0~2.5` | 越大越保守 |

> 多卡不需要你手动拼 torchrun：申请多张卡后平台会设置 `FORCE_TORCHRUN` 与
> `NPROC_PER_NODE`，LLaMA-Factory 自己会切到分布式启动。

## 强化学习

| 参数 | 经验值 | 说明 |
|---|---|---|
| rollout n（组大小） | GRPO / DAPO 建议 8~16 | 越大方差越小、越慢 |
| max_response_length | 长 CoT 给到 8192+ | 直接决定显存与耗时 |
| 学习率 | `1e-6` 量级 | 比 SFT 小两个数量级 |
| clip low / high | DAPO 论文用 `0.2 / 0.28` | 上界放大提升探索 |
| 动态采样 filter_groups | DAPO 建议开 | 丢弃全对/全错的组并重采 |
| 显存占用比例 | 训练 OOM 时先调低这个 | 留给 vLLM 推理引擎的显存 |
| 张量并行度 | 不能大于申请的 GPU 数 | 超了平台会直接拦下 |

### Harbor 任务树做 Agentic RL

选 GRPO/GSPO + `harbor_tasks` 数据集时：

- 框架自动切到 **TRL**，用 `trl.experimental.harbor.HarborSpec` 把任务树变成 RL 环境
- **奖励直接来自任务 verifier 的 reward**，不需要另外写奖励函数
- 需要本机 Docker 可用（沙箱要起容器），并预留足够并发容器配额
- harness 决定 agent 的工具面：`bash`（单一 shell）/ `jupyter`（有状态内核）/ `terminal_notes`（shell + 持久笔记）

---

## 预设

提交页顶部有一排预设，点一下就把整套参数填好，再按需微调：

| 预设 | 适用 |
|---|---|
| LoRA 快速验证 | 单卡跑通，确认数据与模板正确 |
| LoRA 标准训练 | rank 32、全量数据的生产配置 |
| 全参微调 (ZeRO-3) | 多卡全参，7B 以上 |
| DPO · LoRA | 偏好对齐默认配置 |
| verl · GRPO 标准 | 单机多卡 GRPO，vLLM rollout |
| verl · DAPO | 解耦裁剪 + 动态采样，长 CoT 首选 |
| TRL · GRPO + Harbor 环境 | agentic RL |

## 透传参数

「进阶」里可以填任意键值，直接透传给框架，用来试平台还没做成表单的功能：

- **LLaMA-Factory** —— 写进 `train_config.yaml`，覆盖表单生成的同名字段
- **verl** —— 以 `key=value` 追加到 Hydra 命令行末尾
- **TRL** —— 合并进生成脚本里的 Config 参数
