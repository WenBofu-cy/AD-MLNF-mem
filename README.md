# AD-mlnf-mem：自动驾驶 MLNF-Mem 双漏斗记忆中枢

**EM-Core 记忆中枢 · 自动驾驶专项实现 · 51模块正式定稿**

> 版本：V1.0
> 原创提出者：文波福
> 开源协议：CC BY 4.0（知识共享署名 4.0 国际许可证）
> 所属体系：EM-Core AD 自动驾驶认知系统
> 配套仓库：[EM-Core-AD-Spec](https://gitee.com/expanding-research/em-core-ad-spec)（总规范）｜ [AD-ecc-brain](https://gitee.com/expanding-research/ad-ecc-brain)（认知大脑）｜ [AD-mcc-cerebellum](https://gitee.com/expanding-research/ad-mcc-cerebellum)（运动小脑）


## 一、仓库定位

本仓库为 EM-Core 通用智能系统中 **MLNF-Mem 记忆中枢** 的自动驾驶专项实现仓库，承载 51 个记忆模块的接口规范、源代码、伪代码及单元测试。

AD-mlnf-mem 采用双漏斗架构——漏斗一负责驾驶员画像，漏斗二负责自动驾驶自成长经验。双漏斗物理隔离，共用固定知识底座，严格遵循 MLNF-Mem 五层单向晋升与三维重要度驱动核心理论。漏斗外挂扩展区独立运行，不参与记忆沉淀、筛选、晋升与遗忘机制。


## 二、核心架构设计

### 2.1 双漏斗架构

```
┌─────────────────────────────────────────────────┐
│                  AD-mlnf-mem                     │
│                                                  │
│  ┌──────────────────┐  ┌──────────────────────┐  │
│  │  漏斗一            │  │  漏斗二               │  │
│  │  驾驶员画像漏斗     │  │  自动驾驶自成长漏斗    │  │
│  │  (仅人工驾驶模式)   │  │  (仅自动驾驶模式)      │  │
│  └──────────────────┘  └──────────────────────┘  │
│                                                  │
│  ┌────────────────────────────────────────────┐  │
│  │           漏斗外挂扩展区（不参与遗忘机制）      │  │
│  │  世界模型库 │ 交通法规库 │ 情绪意图库 │ 疑问缓存 │  │
│  └────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────┘
```

### 2.2 五层单向记忆晋升通路

```
L1 临时层 → L2 近期层 → L3 中期层 → L4 长期层 → L5 核心层
（本次行程）  （近7日）    （近30日）   （泛化技能）  （永久锁定）
```

记忆仅可单向晋升，不可回退。晋升条件：留存时长达标 + 重要度达标。

### 2.3 三维重要度量化公式

```
重要度 I = I₀ + α·S + β·V + γ·C
```

- **S（安全显著性）**：TTC、ABS/ESC 触发等物理安全信号
- **V（风格匹配度）**：驾驶动作与用户设定风格的契合程度
- **C（复用频次）**：同类场景下该策略被成功执行的次数


## 三、模块分区总览（51个模块）

### 分区一：顶层总控中枢（01–03）

| 编号 | 模块名称 |
|:---:|------|
| 01 | [总控漏斗F0-双漏斗全局调度中枢](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-01-双漏斗全局调度中枢.md) |
| 02 | [漏斗一专属调度单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-02-漏斗一专属调度单元.md) |
| 03 | [漏斗二专属调度单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-03-漏斗二专属调度单元.md) |

### 分区二：漏斗一——驾驶员画像漏斗（04–13）

| 编号 | 模块名称 |
|:---:|------|
| 04 | [驾驶员身份识别单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-04-驾驶员身份识别单元.md) |
| 05 | [子画像槽创建与初始化单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-05-子画像槽创建与初始化单元.md) |
| 06 | [子画像槽数据隔离管控单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-06-子画像槽数据隔离管控单元.md) |
| 07 | [驾驶行为观测记录单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-07-驾驶行为观测记录单元.md) |
| 08 | [上下文场景标记单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-08-上下文场景标记单元.md) |
| 09 | [行为判定标签单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-09-行为判定标签单元.md) |
| 10 | [行为累积统计单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-10-行为累积统计单元.md) |
| 11 | [驾驶辅助提醒生成单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-11-驾驶辅助提醒生成单元.md) |
| 12 | [临时画像槽自动清除单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-12-临时画像槽自动清除单元.md) |
| 13 | [子画像槽长期未活跃提醒单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-13-子画像槽长期未活跃提醒单元.md) |

### 分区三：漏斗二——自动驾驶自成长漏斗（14–43）

**3.1 场景分槽管理（14–19）**

| 编号 | 模块名称 |
|:---:|------|
| 14 | [场景判定与分槽路由单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-14-场景判定与分槽路由单元.md) |
| 15 | [高速巡航槽](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-15-高速巡航槽.md) |
| 16 | [城区路口槽](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-16-城区路口槽.md) |
| 17 | [泊车低速槽](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-17-泊车低速槽.md) |
| 18 | [特殊环境槽](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-18-特殊环境槽.md) |
| 19 | [通用驾驶槽](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-19-通用驾驶槽.md) |

**3.2 五层记忆层级存储（20–30）**

| 编号 | 模块名称 |
|:---:|------|
| 20 | [L1临时层存储单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-20-L1-临时层存储单元.md) |
| 21 | [L1临时层时序衰减单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-21-L1-临时层时序衰减单元.md) |
| 22 | [L2近期层存储单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-22-L2-近期层存储单元.md) |
| 23 | [L2近期层热度统计单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-23-L2-近期层热度统计单元.md) |
| 24 | [L3中期层存储单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-24-L3-中期层存储单元.md) |
| 25 | [L3中期层相似经验归并单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-25-L3-中期层相似经验归并单元.md) |
| 26 | [L4长期层存储单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-26-L4-长期层存储单元.md) |
| 27 | [L4长期层经验抽象提炼单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-27-L4-长期层经验抽象提炼单元.md) |
| 28 | [L5核心层存储单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-28-L5-核心层存储单元.md) |
| 29 | [L5核心层安全规则硬锁定单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-29-L5-核心层安全规则硬锁定单元.md) |
| 30 | [L5核心层防篡改与只读管控单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-30-L5-核心层防篡改与只读管控单元.md) |

**3.3 三维重要度计算引擎（31–37）**

| 编号 | 模块名称 |
|:---:|------|
| 31 | [安全显著性S值计算单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-31-安全显著性S值计算单元.md) |
| 32 | [风格匹配度V值计算单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-32-风格匹配度V值计算单元.md) |
| 33 | [复用频次C值统计单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-33-复用频次C值统计单元.md) |
| 34 | [基础重要度I₀赋值单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-34-基础重要度I0赋值单元.md) |
| 35 | [三维权重系数配置单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-35-三维权重系数配置单元.md) |
| 36 | [综合重要度I值聚合计算单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-36-综合重要度I值聚合计算单元.md) |
| 37 | [重要度增量定时刷新单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-37-重要度增量定时刷新单元.md) |

**3.4 晋升与遗忘执行机制（38–43）**

| 编号 | 模块名称 |
|:---:|------|
| 38 | [晋升双条件判定单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-38-晋升双条件判定单元.md) |
| 39 | [层级单向搬运写入单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-39-层级单向搬运写入单元.md) |
| 40 | [遗忘阈值判定单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-40-遗忘阈值判定单元.md) |
| 41 | [最低复用次数校验单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-41-最低复用次数校验单元.md) |
| 42 | [冗余记忆删除与归档单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-42-冗余记忆删除与归档单元.md) |
| 43 | [失败经验安全仲裁三道校验单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-43-失败经验安全仲裁三道校验单元.md) |

### 分区四：漏斗外挂扩展区（44–47 · 物理隔离）

| 编号 | 模块名称 |
|:---:|------|
| 44 | [独立世界模型库](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-44-独立世界模型库.md) |
| 45 | [交通法律法规库](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-45-交通法律法规库.md) |
| 46 | [道路参与者情绪意图感知库](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-46-道路参与者情绪意图感知库.md) |
| 47 | [疑问缓存库](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-47-疑问缓存库.md) |

### 分区五：存储与系统运维（48–51）

| 编号 | 模块名称 |
|:---:|------|
| 48 | [全局容量配额管控单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-48-全局容量配额管控单元.md) |
| 49 | [存储压缩与冷归档单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-49-存储压缩与冷归档单元.md) |
| 50 | [记忆导入导出与脱敏共享单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-50-记忆导入导出与脱敏共享单元.md) |
| 51 | [记忆变更日志追溯单元](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/spec/ad-51-记忆变更日志追溯单元.md) |


## 四、目录结构

```
AD-mlnf-mem/
├── README.md
├── LICENSE
├── spec/                  ← 51个模块接口规格文档
│   ├── README.md
│   ├── ad-01-双漏斗全局调度中枢.md
│   └── ...
├── src/                   ← 51个模块 Python 源代码
│   ├── [bus.py](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/src/bus.py)
│   ├── [main.py](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/src/main.py)
│   ├── [module_registry.py](https://gitee.com/expanding-research/ad-mlnf-mem/blob/master/src/module_registry.py)
│   └── ...
└── .gitignore
```


## 五、与 AD-ecc-brain、AD-mcc-cerebellum 的协同

| 数据流方向 | 内容 |
|-----------|------|
| [AD-ecc-brain](https://gitee.com/expanding-research/ad-ecc-brain) → 本仓库 | 经验查询请求、经验写入请求、模式切换信号 |
| 本仓库 → [AD-ecc-brain](https://gitee.com/expanding-research/ad-ecc-brain) | 匹配经验列表、查询回执、晋升/遗忘信号 |
| 本仓库 → [AD-mcc-cerebellum](https://gitee.com/expanding-research/ad-mcc-cerebellum) | 运动习惯、体态偏好（仅漏斗二） |

所有跨系统通信统一走 **MemoryBus** 全局记忆总线。


## 六、开源协议

本仓库内容采用 **CC BY 4.0**（知识共享署名 4.0 国际许可证）进行全球开源授权。

- 必须显著保留原作者署名：**文波福**
- 架构首创权永久归属原作者，不可剥夺、不可转移


## 七、学术引用

文波福. AD-mlnf-mem V1.0——自动驾驶 MLNF-Mem 双漏斗记忆中枢模块定稿[EB/OL]. 2026.


## 八、联系方式

- **原创提出者**：文波福
- **邮箱**：710705008@qq.com
- **首发平台**：知乎、CSDN、稀土掘金、GitHub
- **Gitee 组织**：拓研（expanding-research）


## 九、镜像仓库

本仓库同步镜像至 [GitHub](https://github.com/expanding-research/ad-mlnf-mem)