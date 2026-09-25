# 海洋科考采样记录与岸端同步

一个仅使用 Python 标准库实现的离线优先采样记录服务。船端可以按批次上传站位、样本、母样/分样、保管交接和仪器文件元数据；岸端负责幂等接收、冲突隔离、编号分配和确认锁定。

## 运行

```bash
python app.py --init
python app.py --port 8008
```

打开 <http://127.0.0.1:8008>。`--init` 会创建示例航次 `2026-ECS-01`。数据库默认是 `ocean_samples.db`，可用 `--db` 或 `OCEAN_DB` 修改。

## 离线同步

`POST /api/sync` 的每个记录都包含 `type`、`local_uuid`、`revision` 和 `data`。同一条记录重复上传会识别为 `duplicates`，不会重复建库。

```json
{
  "device_id": "tablet-A",
  "records": [
    {"type": "station", "local_uuid": "st-001", "revision": 1, "data": {
      "voyage_id": 1, "station_code": "S-01", "latitude": 30.1,
      "longitude": 122.0, "sampled_at": "2026-09-05T08:30:00+08:00",
      "owner": "member-a"
    }},
    {"type": "sample", "local_uuid": "sample-001", "revision": 1, "data": {
      "station_id": 1, "sample_code": "W-001", "sample_type": "water",
      "depth_m": 5, "storage_condition": "4C", "owner": "member-a"
    }}
  ]
}
```

## 业务规则

- `local_uuid + device_id` 是幂等键；相同内容重复上传直接返回已有记录。
- `revision` 必须递增。旧修订或同修订不同内容会进入冲突表，不会覆盖新数据。
- 站位编号在航次内唯一，样本编号全局唯一；检测到另一设备使用相同编号时分配 `-DUP-xxxx` 后缀并把冲突写入隔离表。
- 记录人员只能修改自己创建的站位/样本，`lead` 可以处理全部记录。
- `lead` 确认样本或站位后，该记录变为只读；发现错误必须通过新记录处理，不能用同步覆盖历史。母样确认锁定后不能再新增子样，但已分出的子样仍可单独交接。
- 子样在母样上生成，编号按母样续接（`W-001` → `W-001-1`），并强制继承母样的航次和站位；撞号时分配 `-DUP` 后缀进隔离清单，不覆盖原记录。
- 保管交接是追加式事件；仪器文件以 SHA-256 去重，原始内容相同但来自不同设备时只保存一次元数据。
- 航次、站位、样本、交接、文件和冲突都保存在 SQLite 中，所有同步批次有审计记录。

## API

- `POST /api/voyages`：创建航次。
- `POST /api/sync`：批量同步离线记录。
- `POST /api/samples/{id}/split`：在已同步母样上分出子样。
- `POST /api/confirm/station/{id}` 或 `/api/confirm/sample/{id}`：负责人确认锁定。
- `GET /api/stations`、`/api/samples`、`/api/custody`、`/api/instrument-files`：查询记录。
- `GET /api/conflicts`：查看编号冲突、旧修订和权限冲突。
- `GET /api/audit`：查看操作审计。

## 母样与分样

科考水样分装成多份子样时，在已同步的母样上生成子样：

```bash
curl -X POST localhost:8008/api/samples/1/split \
  -H 'X-User: member-a' -H 'Content-Type: application/json' -d '{}'
```

- 子样编号按母样续接：母样 `W-001` 依次分出 `W-001-1`、`W-001-2`……；同步通道中带子样关系但不带 `sample_code` 时同样自动续号。
- 子样继承母样的航次、站位、类型、深度、保存条件和负责人（保存条件、深度等可在分样时显式指定）；改挂别的站位（航次或站位与母样不一致）会被拒绝。
- 编号已被占用时**不覆盖原记录**，改分配 `W-001-2-DUP-xxxxxxxx` 后缀并写入隔离清单，返回体带 `requested_code` 和 `conflict`。
- 母样被负责人 `lead` 确认锁定后不能再新增子样（分样接口与同步通道都拒绝）；只有母样（顶层样本）可以分样，子样不能再分出子样。
- 已分出的子样是独立样本，母样锁定后仍可单独登记保管交接（`type: "custody"` 的同步记录直接引用子样 `sample_id`）。

## 测试

```bash
python -m unittest discover -s tests -v
```

测试覆盖完整离线同步、幂等重复、编号冲突、成员越权、旧修订、确认锁定、追加式交接和文件哈希去重。
