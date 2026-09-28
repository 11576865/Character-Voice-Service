# 本机模型扫描（E1）

完整方案见 [实施报告 TXT](CVS_Model_Management_Implementation_Report.txt)。这是只读盘点工具，尚不登记候选、不提升默认、不加载模型。

把 `config/model_roots.example.json` 复制到本机 `data/model-management/roots.json`，将 `path` 改为 GPT-SoVITS 安装根目录的绝对路径。该目录内应有 `GPT_weights_*` 和 `SoVITS_weights_*` 子目录。实际配置和扫描结果留在 Git 忽略的 `data` 中。

在 CVS 根目录运行：

```powershell
.\.venv\Scripts\python.exe -m server.model_scan --roots .\data\model-management\roots.json
```

报告输出为 JSON。要保留本机结果：

```powershell
.\.venv\Scripts\python.exe -m server.model_scan --roots .\data\model-management\roots.json | Out-File -Encoding utf8 .\data\model-management\scan-report.json
```

默认只读文件信息，不生成内容版本 ID。需要完整 SHA-256 时加 `--hash`，会读取所有识别到的权重文件，耗时取决于模型大小及磁盘速度。`--voices` 可以指定另一个角色配置目录。

- `paired`：文件名分组内只有一对有效权重，尚未证明可推理或质量合格。
- `ambiguous`：多轮权重无法唯一配对，等待 manifest 或人工指定。
- `incomplete` / `invalid`：缺少组成文件或发现无效文件。
- `registered`：与现有角色配置的路径关联一致，元数据模式不证明文件内容未变。
- `registered_explicit_pair`：现有配置明确指定了一对文件，可以保留关联，目录内其他候选仍待确认。
- `unregistered`：结构候选尚未关联已有角色；不自动猜角色、语言或参考语音。
- `missing` / `root_unavailable`：文件或目录当前不可用，不删除旧配置。

布局版本依据文件夹名称，只是发现信息，不代表检查过权重内部架构。扫描不反序列化 checkpoint。文件路径变化不影响完整内容 revision；同一路径内容改变会产生不同 revision。

扫描器目前只实现 GPT-SoVITS 目录布局，未识别文件显示在 issues；其他引擎需要新增扫描适配器。完整报告包含本机信息，请勿直接提交到公开仓库。

## 自动关联下一代模型

训练导出目录可放置任意名称以 `.cvs-model.json` 结尾的清单。格式参考 `config/model-export-manifest.example.json`。清单使用相对于 Model Root 的权重路径，并明确 `character_id`、`model_id`、语言和 GPT/SoVITS 文件。扫描器会优先采用清单，并跳过对这两份文件的文件名猜测。

清单应在两份权重写完后最后写入。现有模型布局仍能扫描；当目录里有多轮权重而没有清单时，保持 `ambiguous`，等待明确配对。

完整扫描并登记：

```powershell
.\scripts\manage_models.cmd scan --hash --register
```

首次登记时，现有角色配置中的当前默认权重进入 Registry 的 `default`；其他新版本进入 `candidate`。重复运行不会重复创建 revision。Registry、审计事件和扫描结果保存在 `data/model-management`。

查看、分配和提升：

```powershell
.\scripts\manage_models.cmd list
.\scripts\manage_models.cmd assign rev-... --character march-7th --model-id trained-en --language en --reason "确认训练线"
.\scripts\manage_models.cmd promote rev-... --evaluation eval-... --reason "固定样本与试听通过"
```

没有 Evaluation 时只有显式增加 `--manual-override` 才能提升。提升会核对权重 SHA-256，将角色配置加入 revision 专属模型项并切换默认；原默认进入 `retired`。Registry 中的绝对根目录由本机 `roots.json` 提供，不写入可提交文件。

Evaluation v1 的正式字段见 `config/evaluation-v1.schema.json`，固定原创英文样本见 `config/evaluation-samples-v1.json`。记录支持草稿、运行、完成、取消和失败状态，逐样本保存候选/基线结果，人工评分与最终决策分开。所有样本与所有对照 revision 都有终态结果后，才能标为 completed；只有决策为 promote 的对应 Evaluation 才可用于正常提升。
