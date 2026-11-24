# 实现说明与实测记录

本文记录实现取舍、验证方法与实测结果，供交付核对。规格层面的规则见
`README.md`。

## 结构

```
app/
  gpt.py        纯 GPT/CRC/分区表逻辑：解析、校验、重建、逐字节复验
  converter.py  两份副本的取舍、4 KiB 前置条件、流式复制编排
  export.py     单分区 4Kn 导出：布局、GUID 派生、有界读取、Range 响应
  jobs.py       磁盘任务、单/多工作线程、进度、取消、TTL 清理、导出固定
  main.py       FastAPI 路由与上传流
  samples.py    5 个网页样盘场景（按需生成）
  samplegen.py  造盘/篡改工具（测试、样盘、命令行共用）
  config.py     环境变量配置
tests/          59 个测试（GPT 单元、转换端到端、HTTP 层、分区导出）
tools/make_sample.py  生成/自检提交在仓库里的 64 MiB→16 MiB 样盘
```

## 关键实现

### 主备 GPT 的取舍

`read_gpt_copy()` 对每份副本**独立**做完整校验后才返回：

- 签名、`HeaderCRC32`（计算时把 CRC 字段按规范置零）、`EntriesCRC32`
- `MyLBA` / `AlternateLBA` / `PartitionEntryLBA` 必须是主 1 / 末 LBA / 2
  与末 LBA / 1 / 末 LBA−4
- 128 × 128 字节的表结构、`FirstUsableLBA` / `LastUsableLBA` 不侵入保留区
- 磁盘 GUID 非零

任一项失败只把**那一份**标记为无效，并把原因存进报告；另一份继续检查。
只有两份都通过后才会比较磁盘 GUID、16 KiB 分区表字节和可用区，任一不同即
`GPT_COPIES_CONFLICT`。因此“只有一份有效”和“两份互相矛盾”是两条独立的
代码路径，不存在“主坏了就静默用备份、备份也不可信”的情况。

### 4 KiB 前置条件与报错顺序

`check_partitions()` 返回四类问题：`invalid`（越界/重叠/GUID 重复）、
`overlap`（新 GPT 保留区冲突）、`alignment`（边界未 4 KiB 对齐）、
`warnings`（空表项残留）。

`converter.analyze_source()` 的拒绝顺序：

1. `EMPTY_FILE` / `SOURCE_NOT_512` / `HYBRID_MBR`
2. `BOTH_GPT_INVALID` / `GPT_COPIES_CONFLICT`
3. `PARTITION_INVALID`
4. `DISK_TOO_SMALL`（4 KiB GPT 至少需要 12 个扇区 = 48 KiB）
5. `DISK_NOT_ALIGNED`
6. `GPT_REGION_CONFLICT`（有 overlap 时优先）
7. `PARTITION_NOT_ALIGNED`

第 5 步的 `details.problems` 同时包含 overlap 与 alignment 条目。原因：一个
512 扇区源盘上的分区若伸进 4 KiB 备 GPT 区，其结束边界几乎必然也非 4 KiB
对齐；只报第一条会让用户以为改完对齐就没事，实际上空间仍然冲突。

### 重建与保留

`build_layout()` 只改写分区项的 `StartingLBA`/`EndingLBA`（字节范围除以
4096），类型 GUID、唯一 GUID、属性、名称原样保留；`write_layout()` 清零
MBR 之后到 `FirstUsableLBA` 的全部旧数据以及盘尾保留区，再写新结构。因此
旧 512 扇区 GPT 的残渣不会留在成品里。

### 复验

`verify_layout_image()` 重新打开成品文件，从头解析主备 GPT、保护 MBR，
对照重建布局逐字节比对保留区，并把每个分区的 SHA-256 与**复制期间从源盘
算出**的哈希比对。哈希在 `copy_with_partition_hashes()` 的同一遍顺序读里
完成，不会额外整盘读源。复验失败时工作线程删除输出，下载接口只认
`status == "done"`。

### 内存

上传 4 MiB 分块写盘；复制按 `MIGRATE_COPY_CHUNK_BYTES` 分块
`pread`/`pwrite`，128 个 SHA-256 上下文按块更新；GPT 只在内存里保留
16 KiB 表 + 少量扇区。峰值内存与盘长无关。

### 单分区导出

`export.py` 不写盘、不复制成品，按请求范围实时生成字节流：

- `plan_partition_export()` 取分区原始 128 字节表项，只改唯一 GUID，其余
  字段（类型、属性、名称、表内序号）原样带入 `build_layout()`，由后者把
  LBA 换算到 4096 字节扇区。
- 导出镜像 = 主 GPT 区（24 KiB）+ 全零填充至分区起点 + 原样分区 + 备 GPT
  区（20 KiB，紧接分区末尾）。长度 = 分区结束字节 + 20480。
- GUID 用 `uuid5` 从任务 id、分区号与原 GUID 派生，**无随机数**，因此同一
  任务同一分区的多次（含并发）请求得到完全相同的字节；这是 Range 拼接
  一致性的基础。
- `ExportSource.read()` 在一次调用内分别叠加主 GPT 区、`pread` 分区段、
  备 GPT 区，其余保持零。读取块大小由 `MIGRATE_COPY_CHUNK_BYTES` 控制，
  固定缓冲区，峰值内存与导出长度无关。
- HTTP 层 `ExportResponse` 自行解析单段 `Range`，永远写
  `Content-Length`/`Content-Range`/`Accept-Ranges`；`HEAD` 只回头部并立即
  关闭源。导出期间不读取任务目录里的其它文件，也不新建文件。
- 生命周期：`JobManager.open_export()` 先 `open()` 成品并登记
  `_active_exports`；之后再删除任务只会把目录标记为待清理
  （`_pending_cleanup`），文件描述符仍可读完。最后一个导出 `close()` 时
  才 `rmtree`。`discard()` 先置 `Job.discarded`，阻止工作线程在删除后重新
  写 `job.json`（此前会导致目录残留）。

## 实测记录

环境：Linux 7.0.0 x86_64，Python 3.11，FastAPI 0.165，uvicorn 0.54。
本机没有 docker/podman，**镜像未实际构建**（见“待办”）；应用以 uvicorn
直接启动验证。

### 1. 自动化测试

```
$ python3 -m pytest -q
59 passed, 1 warning in 76.53s
```

覆盖：正常转换、主表坏、备表坏、双表坏、磁盘 GUID 冲突、分区项冲突、
混合 MBR、分区未对齐、盘长未对齐、新主/备 GPT 空间冲突、保护 MBR 缺失
重建、空表项残留、GUID/属性/名称保留、成品被篡改（GPT 区与分区内容）、
复制读取块大小上限、盘长不足以容纳 4 KiB GPT、HTTP 上传/下载/取消/TTL/
孤儿目录/大小上限/非 ASCII 文件名；以及单分区导出：独立解析、字节位置与
内容一致、类型/属性/名称保留、两个 GUID 均更换、备份 GPT 紧接分区、其余
字节为零、原成品不被改动、任务目录不新增文件、码段拼接与整盘下载逐字节
相同、开区间/后缀/416/HEAD、删除或 TTL 后新请求 404 而已开始传输可完成、
导出读取块大小上限。

### 2. 试跑：单表损坏

样盘 `one-bad-copy`（主表头 CRC 被改）通过网页同款接口上传：

```
POST /api/jobs  -> 202
GET  /api/jobs/{id} -> status=done, used_gpt_copy=backup
  primary: valid=false  "主 GPT表头 CRC32 不符（存储 efbeadde，实算 1976e753）"
  backup:  valid=true
  检查项 10 项全部通过
GET  /api/jobs/{id}/download -> 200, 16777216 字节
```

成品用**不依赖本应用代码**的独立解析脚本复验：两份 GPT 的签名、表头 CRC、
分区项 CRC、LBA 位置、可用区、128×128 结构、分区对齐全部通过，磁盘 GUID
与分区 GUID 与源盘一致。

### 3. 试跑：空间冲突

`tail-collision` 样盘（分区伸入 4 KiB 备 GPT 保留区）：

```
POST /api/jobs -> 422 GPT_REGION_CONFLICT
  - 分区 1 结束于字节 16760320（不含），与 4 KiB 新备 GPT 保留区
    [16756736, 16777216) 重叠
  - 分区 1 结束边界（不含）16760320 未对齐到 4096 字节（余 3584）
```

`unaligned` → `422 PARTITION_NOT_ALIGNED`；`both-bad` → `422
BOTH_GPT_INVALID`（附两份副本各自的失败原因）。

### 4. 内存实测（同一进程 `ru_maxrss`）

| 源盘 | 分块 | 峰值 RSS |
| --- | --- | --- |
| 64 MiB | 4 MiB | 23 872 KiB |
| 1 GiB | 4 MiB | 23 132 KiB |

1 GiB 转换 + 复验约 22 s（本机磁盘）。峰值内存不随盘长增长。

### 5. 独立解析器复验

对正常样盘与单表损坏样盘的成品各跑一遍独立校验脚本（纯 `os.pread` +
`struct` + `zlib`，不 import 本应用任何模块），结果 `PASS`。

### 6. 单分区导出实测

16 MiB 正常样盘转换完成后导出两个分区，用同一独立脚本（额外断言“备 GPT
紧接分区”）复验：

```
PASS export-1.img size 4214784  disk_guid C8BAFD39-…-FD1D8F06F9BF
     part 1 start 1048576 end_excl 4194304 name 'EFI'
     unique C597A465-…-EF3D4EB88C05（源 00000001-1111-4222-8333-000000000001）
PASS export-2.img size 14700544 disk_guid A00B4348-…-133621F7FEE5
     part 2 start 5242880 end_excl 14680064 name 'data'
     unique E1167D73-…-55CBC2FBDA1D（源 00000002-…-000000000002）
```

- 分区内容的 SHA-256 与整盘成品对应字节范围一致：
  `429fe2a258aae020…`、`65aac7fc6733b63b…`。
- 二次整盘下载与导出前后内容逐字节相同（原成品未被改动）。
- 每 777 777 字节切一段（6 段 / 19 段）分别 GET，按序拼接 == 一次整盘
  下载；另测 `bytes=a-`、`bytes=-n`、越界 416、`HEAD`。
- 删除任务后再次请求 404，已打开的 `ExportSource` 仍读完整个镜像，之后
  任务目录才消失。

### 7. 导出内存实测（同一进程 `ru_maxrss`）

| 源盘 | 转换后 | 导出两个分区后 |
| --- | --- | --- |
| 64 MiB | 33 576 KiB | 41 044 KiB |
| 1 GiB | 33 188 KiB | 33 188 KiB |

逐段 Range 读取 1 GiB 分区的全部字节，峰值内存不随导出长度增长。

## 待办 / 已知限制

- **Docker 镜像未在本机构建验证**（无 docker/podman 可用）。Dockerfile 与
  compose 已按非 root、只读根文件系统、`cap_drop: ALL` 编写，需在具备
  Docker 的机器上执行 `docker compose up --build` 做一次冒烟；单分区导出
  不需要新的卷或端口。
- 单分区导出不支持一次请求多段 Range（`multipart/byteranges`）；按 RFC
  9110 回退为整文件。
- 只支持 GPT；MBR-only 与混合 MBR 明确拒绝，不做猜测性转换。
- 只接受标准 128×128 分区表与标准 GPT 位置；非标准布局的副本会判无效。
- 任务表在进程内存中，重启后未完成任务不可恢复（目录按 TTL 清理）；
  没有跨副本的任务存储/队列，故默认单 worker。
- 未做认证、限流与磁盘配额；公网部署前需要补。
- 尚未在真实 4Kn 虚拟机/硬件中验证引导，只做了镜像级校验。
