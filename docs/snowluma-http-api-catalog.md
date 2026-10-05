# SnowLuma OneBot HTTP API 与 WebSocket 事件核对

核对日期：2026-10-04。性质：**只读外部核对**，未连接任何真实端点、未读取凭证或运行
配置、未发群。来源为两个一手材料：

- 官方文档站 `snowluma.github.io/zh`（API 参考页与配置页，最后更新于 2026-10-04）；
- GitHub `SnowLuma/SnowLuma` main 分支源码（发布版本 v1.14.21），集中于
  `packages/onebot/src/`（network、event-converter、types）。

本文档**不覆盖也不替代**归档
`openspec/changes/archive/2026-10-04-direct-snowluma-websocket-forwarding/verification/`
中的 U1–U8 结论：凡涉及真实部署版本、账号字段含义、发送字段、图片安全获取与回执
登记的条目，仍以归档清单的未确认状态为准，须经获准联验逐项确认。

## 1. HTTP API 总览

SnowLuma 的 HTTP API 与 OneBot v11 兼容，与 WebSocket 服务端**共用同一套动作注册表和
应答信封**：

- 调用方式：`POST /{动作名}`，请求体 `Content-Type: application/json`。
- 默认地址：`http://127.0.0.1:3000/`（Docker 网络内为 `http://snowluma:3000/`）。
- 鉴权（配置了 Token 时）：`Authorization: Bearer <token>`，或查询参数
  `?access_token=`。HTTP 与 WS 两个适配器可能各自持有独立 Token。
- 应答信封：`{"status": "ok", "retcode": 0, "data": ..., "message": "", "wording": ""}`；
  `retcode` 为 0 才是成功，HTTP 200 也可能是业务失败。
- 服务端源码中的错误码常量：`ACTION_FAILED=100`、`INTERNAL_ERROR=1200`、
  `BAD_REQUEST=1400`、`UNKNOWN_ACTION=1404`（`packages/onebot/src/types.ts`）。
- 应答信封与 ws 帧一致，意味着插件现有的响应结构校验（只认合法成功信封、拒绝事件
  与契约外状态）可以原样复用。

## 2. 全量动作清单（195 个，12 类）

清单由官方文档目录生成源 `catalog.json` 程序化整理，与文档站「全部动作」一致。
「插件固定动作」指当前插件直连契约冻结的四个动作；「同类发送/转发动作」是官方存在
但插件按契约不换用的通道。

### 信息（5 个）

| 动作名 | 说明 |
| --- | --- |
| `can_send_image` | 能力查询结果。 |
| `can_send_record` | 能力查询结果。 |
| `get_login_info` **（插件固定动作）** | 当前登录账号的 QQ 号与昵称。 |
| `get_status` | 运行状态。online 表示账号在线；good 表示已确认的收发链路健康；time 为实例运行秒数。 |
| `get_version_info` | 实现与协议版本信息。 |

### 消息（6 个）

| 动作名 | 说明 |
| --- | --- |
| `delete_msg` | 撤回消息 |
| `get_msg` **（插件固定动作）** | 获取消息 |
| `send_custom_face` | 发送账号收藏表情（emoji_id 或 MD5），保留表情显示样式 |
| `send_group_msg`（同类发送/转发动作，插件不换用） | 发送群消息 |
| `send_msg` **（插件固定动作）** | 发送消息（按 message_type/群号 自动路由群聊或私聊） |
| `send_private_msg`（同类发送/转发动作，插件不换用） | 发送私聊消息 |

### 好友（3 个）

| 动作名 | 说明 |
| --- | --- |
| `delete_friend` | 删除好友 |
| `get_friend_list` | 获取好友列表 |
| `get_stranger_info` | 获取陌生人信息 |

### 群信息（6 个）

| 动作名 | 说明 |
| --- | --- |
| `get_group_honor_info` | 获取群荣誉信息 |
| `get_group_info` | 获取群信息 |
| `get_group_list` **（插件固定动作）** | 获取群列表 |
| `get_group_member_info` | 获取群成员信息 |
| `get_group_member_list` | 获取群成员列表 |
| `get_group_system_msg` | 获取群系统消息 |

### 群管理（18 个）

| 动作名 | 说明 |
| --- | --- |
| `get_group_admin_settings` | 获取群管理设置的当前值 |
| `set_group_add_option` | 设置加群选项 |
| `set_group_admin` | 设置/取消管理员 |
| `set_group_anonymous` | 匿名开关（未实现，返回 ok） |
| `set_group_anonymous_ban` | 匿名禁言（未实现，返回 ok） |
| `set_group_ban` | 禁言群成员（duration=0 解除） |
| `set_group_card` | 设置群名片（空字符串清除） |
| `set_group_kick` | 踢出群成员 |
| `set_group_kick_members` | 批量踢出群成员 |
| `set_group_leave` | 退群 |
| `set_group_member_invite_policy` | 设置群成员邀请策略 |
| `set_group_member_permissions` | 设置群成员权限（仅群主可改） |
| `set_group_name` | 设置群名 |
| `set_group_new_member_history_visibility` | 设置新成员是否可查看历史消息 |
| `set_group_portrait` | 设置群头像 |
| `set_group_search` | 设置群被搜索方式（群指纹 / 群号搜索开关） |
| `set_group_special_title` | 设置群头衔 |
| `set_group_whole_ban` | 全员禁言开关 |

### 群文件（12 个）

| 动作名 | 说明 |
| --- | --- |
| `create_group_file_folder` | 创建群文件夹 |
| `delete_group_file` | 删除群文件 |
| `delete_group_file_folder` | 删除群文件夹 |
| `get_group_file_url` | 获取群文件下载链接 |
| `get_group_files_by_folder` | 获取群子目录文件列表 |
| `get_group_root_files` | 获取群根目录文件列表 |
| `get_private_file_url` | 获取私聊文件下载链接 |
| `move_group_file` | 移动群文件 |
| `rename_group_file` | 重命名群文件 |
| `rename_group_file_folder` | 重命名群文件夹 |
| `upload_group_file` | 上传群文件 |
| `upload_private_file` | 上传私聊文件 |

### 请求（2 个）

| 动作名 | 说明 |
| --- | --- |
| `set_friend_add_request` | 处理好友添加请求 |
| `set_group_add_request` | 处理加群请求 |

### 扩展（115 个）

| 动作名 | 说明 |
| --- | --- |
| `add_custom_face` | 添加收藏表情 |
| `bot_exit` | 退出机器人 |
| `cancel_group_todo` | 取消群待办 |
| `check_url_safely` | 检查链接安全性 |
| `clean_cache` | 清理缓存 |
| `click_inline_keyboard_button` | 点击内联键盘按钮 |
| `complete_group_todo` | 完成群待办 |
| `create_collection` | 创建收藏（未实现） |
| `create_flash_task` | 创建闪传任务 |
| `delete_custom_face` | 删除收藏表情 |
| `delete_essence_msg` | 移除精华消息 |
| `delete_flash_file` | 删除闪传文件 |
| `delete_group_folder` | 删除群文件夹 |
| `download_file` | 下载文件（url 或 base64）到 data/downloads |
| `download_fileset` | 解析闪传文件下载直链（不下载，由调用方实现下载） |
| `fetch_custom_face` | 获取自定义表情 |
| `fetch_custom_face_detail` | 获取自定义表情详情 |
| `fetch_emoji_like` | 获取表情回应用户（NapCat 分页） |
| `fetch_ptt_text` | 获取语音转文字结果 |
| `forward_friend_single_msg`（同类发送/转发动作，插件不换用） | 转发单条消息给好友 |
| `forward_group_single_msg`（同类发送/转发动作，插件不换用） | 转发单条消息到群 |
| `friend_poke` | 好友拍一拍 |
| `get_ai_characters` | 获取 AI 语音角色 |
| `get_ai_record` | 生成 AI 语音 |
| `get_clientkey` | 获取 clientkey |
| `get_collection_list` | 获取收藏列表 |
| `get_cookies` | 获取 Cookies |
| `get_credentials` | 获取凭证 |
| `get_csrf_token` | 获取 CSRF 令牌 |
| `get_doubt_friends_add_request` | 获取可疑好友申请 |
| `get_emoji_likes` | 获取表情回应用户 |
| `get_essence_msg_list` | 获取精华消息列表 |
| `get_file` | 获取文件信息（仅图片/语音缓存；群文件请用 get_group_file_url） |
| `get_fileset_id` | 从 QQ 闪传分享码或官方分享链接获取 fileset_id |
| `get_fileset_info` | 获取文件集信息 |
| `get_flash_file_list` | 获取闪传文件列表 |
| `get_flash_file_url` | 获取闪传文件链接 |
| `get_forward_msg`（同类发送/转发动作，插件不换用） | 获取合并转发消息（id 或 message_id） |
| `get_friend_msg_history` | 获取好友消息历史（无锚点时从服务器获取最新双向记录） |
| `get_friends_with_category` | 获取分组好友列表 |
| `get_group_at_all_remain` | 获取群 @全体成员 剩余次数 |
| `get_group_detail_info` | 获取群详细信息 |
| `get_group_file_system_info` | 获取群文件系统信息 |
| `get_group_ignore_add_request` | 获取被忽略的入群请求（NapCat） |
| `get_group_ignored_notifies` | 获取被过滤的入群请求 |
| `get_group_info_ex` | 获取群信息（扩展） |
| `get_group_msg_history` | 获取群消息历史 |
| `get_group_shut_list` | 获取群禁言列表 |
| `get_group_signed_list` | 获取群今日打卡列表 |
| `get_group_todo_list` | 获取群待办列表 |
| `get_image` | 获取图片信息 |
| `get_mini_app_ark` | 获取小程序卡片 ark |
| `get_msg_emoji_likes` | 获取一条消息的全部表情回应 |
| `get_online_clients` | 获取在线客户端 |
| `get_profile_like` | 获取资料点赞 |
| `get_recent_contact` | 获取最近会话（占位） |
| `get_record` | 获取语音信息；传 out_format 则服务端转码并附带 base64 |
| `get_rkey` | 获取下载 rkey |
| `get_rkey_server` | 获取 rkey 服务器信息 |
| `get_share_link` | 获取文件分享链接 |
| `get_unidirectional_friend_list` | 获取单向好友列表 |
| `group_poke` | 群拍一拍 |
| `list_filesets` | 列出当前账号的所有闪传文件集 |
| `mark_group_msg_as_read` | 标记群消息已读 |
| `mark_msg_as_read` | 标记消息已读（群聊/私聊自动路由） |
| `mark_private_msg_as_read` | 标记私聊消息已读 |
| `modify_custom_face` | 修改收藏表情备注 |
| `move_custom_face_to_front` | 收藏表情移到最前 |
| `nc_get_packet_status` | 获取 packet 状态（占位） |
| `nc_get_user_status` | 获取用户在线/扩展状态 |
| `ocr_image` | OCR 图片（服务端，需图片 URL 或已缓存的图片 file_id） |
| `rename_flash_file` | 重命名闪传文件 |
| `request_decrypt_key` | 请求数据库解密密钥 |
| `send_ark_share` | 分享用户/群 Ark 卡片（NapCat 标准名） |
| `send_flash_msg` | 发送闪传消息（私聊或群聊，引用 fileset_id 让对端下载） |
| `send_forward_msg`（同类发送/转发动作，插件不换用） | 发送合并转发（按 message_type/群号自动路由） |
| `send_group_ai_record` | 发送 AI 语音到群 |
| `send_group_ark_share` | 分享群 Ark 卡片（NapCat 标准名） |
| `send_group_forward_msg`（同类发送/转发动作，插件不换用） | 发送群合并转发 |
| `send_like` | 点赞 |
| `send_packet` | 发送原始 SSO 包（cmd + hex data） |
| `send_poke` | 拍一拍（群聊/私聊自动路由） |
| `send_private_forward_msg`（同类发送/转发动作，插件不换用） | 发送私聊合并转发 |
| `send_tuwen_ark` | 发送图文 Ark 卡片（私聊/群聊） |
| `set_diy_online_status` | 设置自定义在线状态 |
| `set_doubt_friends_add_request` | 处理可疑好友申请 |
| `set_essence_msg` | 设置精华消息 |
| `set_friend_remark` | 设置好友备注 |
| `set_friends_category` | 移动好友到指定分组 |
| `set_group_reaction` | 群聊表情回应 |
| `set_group_remark` | 设置群备注 |
| `set_group_robot_add_option` | 设置群机器人加群选项 |
| `set_group_sign` | 群签到 |
| `set_group_todo` | 设置群待办 |
| `set_input_status` | 设置输入状态 |
| `set_msg_emoji_like` | 设置消息表情回应 |
| `set_online_status` | 设置在线状态 |
| `set_qq_avatar` | 设置 QQ 头像 |
| `set_qq_profile` | 设置 QQ 资料 |
| `set_restart` | 重启（不支持） |
| `set_self_longnick` | 设置个性签名（longNick/long_nick，严格 string） |
| `share_group_ex` | 分享群 Ark 卡片 |
| `share_peer` | 分享用户/群 Ark 卡片 |
| `trans_group_file` | 转存群文件 |
| `translate_en2zh` | 英译中 |
| `upload_forward_msg`（同类发送/转发动作，插件不换用） | 上传转发消息 |
| `upload_foward_msg`（同类发送/转发动作，插件不换用） | 上传转发消息（别名拼写） |
| `_del_group_notice` | 删除群公告（fid 或 notice_id 二选一） |
| `_get_friend_dress` | 获取指定 QQ 号正在使用的个性装扮（挂件/名片/来电/输入状态等） |
| `_get_group_notice` | 获取群公告 |
| `_get_model_show` | 获取机型展示（兼容 mock） |
| `.get_word_slices` | 分词（未实现） |
| `_mark_all_as_read` | 标记全部已读 |
| `_send_group_notice` | 发送群公告（支持置顶、弹窗、新成员、群昵称引导与确认） |
| `_set_model_show` | 设置机型展示（占位） |

### 群相册（9 个）

| 动作名 | 说明 |
| --- | --- |
| `cancel_group_album_media_like` | （官方目录未附中文说明） |
| `del_group_album_media` | 删除群相册图片或视频 |
| `do_group_album_comment` | （官方目录未附中文说明） |
| `get_group_album_list` | 群相册列表数组，每项为一个相册的基本信息。 |
| `get_group_album_media_list` | 相册图片/视频列表及下一页分页游标；视频项含 id、url、cover、尺寸、时长和多规格地址。 |
| `get_qun_album_list` | NapCat 风格相册列表封套：{album_list, attach_info, has_more}。 |
| `set_group_album_media_like` | （官方目录未附中文说明） |
| `upload_image_to_qun_album` | （官方目录未附中文说明） |
| `upload_video_to_qun_album` | 上传视频到群相册 |

### 空间（9 个）

| 动作名 | 说明 |
| --- | --- |
| `comment_qzone` | 评论一条说说（QQ 空间，支持纯文字或带图；传 images 自动上传） |
| `delete_qzone_msg` | 删除一条说说（QQ 空间，按 tid） |
| `get_qzone_feeds` | 获取 QQ 空间好友动态（feed）；page_num 仅首页可靠，深翻页需时间游标（暂未实现） |
| `get_qzone_msg_list` | 获取 QQ 空间说说列表（默认机器人自己的空间） |
| `like_qzone` | 给一条说说点赞（QQ 空间） |
| `send_qzone_msg` | 发表说说（QQ 空间，支持纯文字或带图；传 images 自动上传；可设置查看权限） |
| `set_qzone_ban` | 拉黑或解除拉黑某人（修改机器人自身 QQ 空间黑名单；enable=true 拉黑，false 解除） |
| `set_qzone_msg_right` | 修改一条已发说说的查看权限（QQ 空间，按 tid） |
| `unlike_qzone` | 取消对一条说说的点赞（QQ 空间；取消赞端点待真机核实） |

### 系统表情（4 个）

| 动作名 | 说明 |
| --- | --- |
| `fetch_face_entity` | 按编号查询 QQ 系统表情 |
| `fetch_super_face_id` | 判断 QQ 系统表情是否使用超级表情格式 |
| `fetch_sys_faces` | 获取 QQ 系统表情目录 |
| `search_sys_faces` | 搜索 QQ 系统表情 |

### 流式接口（6 个）

| 动作名 | 说明 |
| --- | --- |
| `clean_stream_temp_file` | 清理流式传输临时文件(仅清理 stream 上传/下载目录) |
| `download_file_image_stream` | 以流式方式下载图片(缓存图片 id / URL / stream 目录本地文件) |
| `download_file_record_stream` | 以流式方式下载语音(缓存语音 id / URL / stream 目录本地文件) |
| `download_file_stream` | 以流式方式下载文件(stream 目录本地文件 / URL / 缓存媒体) |
| `test_download_stream` | 测试下载流(推送 10 个数据帧,验证流式传输,不触达 QQ) |
| `upload_file_stream` | 以流式分块方式上传文件到机器人本地(返回可用于发送的本地路径) |

## 3. WebSocket 服务端事件清单

ws 服务端默认 `ws://<host>:3001/`，path `/`，role `Universal`（该角色下 `/api` 按
Api、`/event` 按 Event 处理）。事件经 `packages/onebot/src/event-converter/` 生成：

| post_type | 事件 |
| --- | --- |
| message / message_sent | 群消息（group/normal，含 `group_name`）、好友消息（private/friend）、临时会话（private/group，sender 带来源 `group_id`）；自发消息为 `message_sent`，仅 `reportSelfMessage=true` 的适配器收到 |
| notice（18 种） | `group_increase`（approve/invite）、`group_decrease`（leave/kick/kick_me/disband）、`group_ban`（ban/lift_ban）、`group_admin`（set/unset）、`friend_recall`、`group_recall`、`notify/poke`（好友+群两种）、`essence`（add/delete）、`group_upload`、`friend_add`、`group_card`、`bot_offline`、`notify/profile_like`、`notify/title`、`notify/group_name`、`notify/input_status`、`group_msg_emoji_like`（add/remove） |
| request（2 种） | `friend`、`group`（sub_type 为 invite） |
| meta_event | 连接建立时推送 lifecycle bootstrap 帧，适配器关闭时推 disable lifecycle 帧 |

传输层行为（源码 `network/ws-server-connections.ts`、`ws-server-adapter.ts`）：

- 动作响应经**发起连接的发送回调**单独回写，不广播；事件才广播给全部
  Event/Universal 连接。
- 升级前鉴权失败返回 401、路径不匹配返回 400；Token 轮换时已授权连接立即被
  1008 断开。
- 服务端每 30 秒 ping，约 90 秒无应答终止半开连接。
- `httpServers` 适配器可配 `enableWebSocket`，HTTP 端口也可能挂 ws 升级。

## 4. 以 HTTP 端点替换当前 ws 直连（实测结论）

### 4.1 结论

**替换链路所需的全部 HTTP 动作已实测通过（矩阵见第 6 节）**：HTTP 可作为直连通道，
且传输层（echo 配对、事件广播隔离、长连接管理）可整体删除。但这仍是对 ADR 0007
与已归档实施变更的架构级变更，按项目流程应另起 OpenSpec 变更修订主规格后返工，
未授权前不改代码。

### 4.2 实测确认的事实

- **动作与信封一致**：`get_login_info` / `get_group_list` / `get_msg` / `send_msg`
  四个固定动作与全部候选替换动作在 HTTP 上复现，请求体与 ws 帧同构，应答信封相同；
  插件现有的响应校验（只认合法成功信封）可原样复用。
- **`message_id` 可以为负**：`send_msg` 实测返回负数 ID（如 -1289506878），
  `forward_group_single_msg` 与图片消息返回正数。`get_msg` 按原值（带符号）查询
  命中，字符串形式也接受；实现不得对 ID 做正整数假设，比对照旧（插件的
  `canonical_message_id` 逐字保留语义正好覆盖）。
- **回执四项核验可过**：三类发送路径（`send_msg` 文本/图片/forward 段、
  `send_forward_msg`）的产物经 `get_msg` 复核，`message_id` / `message_type=group` /
  目标群 / 账号 / 正整数 `message_seq` 全部一致。
- **错误信封分类一致**：未知动作 1404、参数错误 1400、认证失败 HTTP 401；不存在
  的消息返回 `retcode=100 message not found`（业务失败，不是传输异常）。
- **echo 关联、事件隔离、连接代次不再适用**：HTTP 天然一一对应；「断线不重放」
  语义改为「超时/断连不重试」，已提交但无回执的 unknown 分类保留。

### 4.3 HTTP 必须新增的约束（均有实测依据）

1. **必须显式 `allow_redirects=False`**。本地实测 aiohttp 3.14.3：对 307/308 默认
   重发 POST、`Authorization` 头与请求体到新 Location——**令牌与评论正文双重外泄**；
   302 虽在当前版本被改写为 GET 且剥离凭证，同样不应跟随。ws 侧由 trace 拒绝握手
   重定向，HTTP 侧须等价措施，不把重定向当自动发现。
2. **body 实际上限约 2–3MB**：100KB / 1MB / 2MB 可过（1MB 起超长文本被业务层
   拒绝，`retcode=100`），3MB 起服务端**直接关闭连接、无结构化错误码**。客户端
   需自设 body 上限并在发送前拒绝（ws 侧 4MiB 帧上限的同类约束，数值不同）。
3. **端点与 Token 分离**：HTTP（默认 3000）与 ws（3001）可能各自持 Token；
   `[snowluma]` 配置键需改为 HTTP 地址语义，操作者按部署填写，不自动选端口。
4. **明文 http 仅在目标解析为回环/私有/链路本地时允许**，公网必须 https 验证；
   本次测试经 arclinux 本机回环完成。

### 4.4 对 U1–U8 的增量影响

- **U1（认证）**：本实例确认 Bearer 头被接受，失败返回 401。
- **U2（多客户端/响应归属）**：HTTP 无长连接广播，问题不存在；帧/连接限制未确认。
- **U3–U8**：维持未确认。其中 **U5 有新事实**：服务端接受 `image` 段的公链
  https URL，自行下载后转为 QQ 多媒体资源（回执记录中是腾讯 CDN 签名地址，
  本文不记录）。这使插件侧的边界更重要——**只提供来自可信 OneBot 源消息的材料，
  绝不把调用者任意 URL 交给服务端下载**。注意该事实只覆盖**图片直发路径**；
  卡片路径（`forward_group_single_msg` 重建跨群含图卡片）存在相反方向的实测
  失败，见第 6.5 节。
- 以上均为单实例、单账号、测试群的快照，不等于全量联验。

### 4.5 影响面（若启动该变更）

- `snowluma_transport.py`：重写为 HTTP 发送（保留固定动作集、安全端点判定、
  有界预算常量、令牌仅经认证头）；删除接收循环/echo/事件判别主体。
- `snowluma_client.py`：响应校验与回执核验逻辑基本不动；负数 ID 照比。
- 主规格 `snowluma-direct-client`：「正向连接与受控认证」的 ws 限定、「动作关联
  与事件隔离」整节改写，新增 body 上限与禁重定向条款。
- `docs/adr/0007` 之后追加新 ADR；`config.py`/`config.toml` 的 `[snowluma]` 键
  与 `config_version` 评估。
- 测试按 HTTP 重写，仍只用本地合成端点；依赖不变（aiohttp）。

## 5. 合并转发动作与「换动作」评估（实测修正）

### 5.1 各动作实测状态

| 动作 | 实测（arclinux） | 可核验回执 | 产物形态 |
| --- | --- | --- | --- |
| `send_msg` + `forward` 段引用 `upload_forward_msg` 的 res_id | 通过 | 是（seq 15315） | 引用已上传资源 |
| `send_forward_msg` 自定义节点 | 通过 | 是（seq 15316） | 服务端自建 res_id |
| `forward_group_single_msg`（已有 message_id） | 通过 | 是（seq 15313） | 服务端重建 res_id |
| `upload_forward_msg` | 仅上传 | **否**（`message_id=0`） | 返回 `res_id`/`forward_id` |

### 5.2 对两个设想的修正结论

- **合并转发**：`send_forward_msg` 与 `forward_group_single_msg` 都能返回可核验的
  真实 ID，回执链不再是障碍；`send_msg` + forward 段（引用 `upload_forward_msg`
  的 res_id）同样可用且最贴近现行「资源引用」语义。三者的代价变为**产品语义**：
  自定义节点卡片（发送者署名可控）、单条转发、自建资源副本，均与「源消息原资源
  直引」不同，须由规格显式选择。
- **图片/文字走 `upload_forward_msg`**：链路可用但**不建议**。它只上传不发送，
  必须再发一次；「上传成功、发送未提交」成为新中间态，与现行「一次源最多提交
  一个发送动作」和 unknown/failed 分类冲突；且普通消息卡片化是展示形态变更。
  普通图/文继续用 `send_msg` 段直发（含图片段公链场景，见 U5 边界）。

## 6. 实测记录（2026-10-04，arclinux，已获授权）

授权范围：SSH arclinux 核实 HTTP 端点；向指定测试群发送若干条探测消息。
全程不回显、不落盘凭证；本文不记录真实账号、群名、令牌与签名媒体地址。

### 6.1 环境快照

- 容器 `maim-bot-snowluma` 运行中，端口 3000(HTTP)/3001(ws)/5099 已映射；
  测试在容器内以 `http://127.0.0.1:3000` 进行。
- `get_version_info`：SnowLuma **1.14.17-node**、协议 v11；`get_status`：
  `online=true, good=true`；`get_login_info` 返回真实账号（不记录）。
- `get_group_list` 正常，测试群（3 人群）在名册；`get_group_msg_history` 正常。

### 6.2 动作矩阵

| 动作 | 用例 | 结果 |
| --- | --- | --- |
| `get_version_info` / `get_status` / `get_login_info` | 只读 | ok；Bearer 头认证被接受（U1 本实例确认） |
| `get_group_list` | 只读 | ok |
| `get_group_msg_history` | group_id + count | ok，返回 messages 数组 |
| `get_msg` | 正数 / 负数 / 字符串 ID | 均 ok；不存在返回 `retcode=100` |
| `send_msg` | 纯文本群消息 | ok；`get_msg` 四项核验过（seq 15314） |
| `send_msg` | text + image（https 公链） | ok；服务端下载后转 QQ 多媒体资源（seq 15317） |
| `send_msg` | forward 段引用 upload 的 res_id | ok；seq 15315 |
| `send_forward_msg` | 两节点自定义卡片 | ok；返回 `message_id` + `res_id` + `forward_id`；seq 15316 |
| `forward_group_single_msg` | 已有卡片 message_id 转发 | ok；服务端重建 res_id；seq 15313 |
| `upload_forward_msg` | 自定义节点上传 | ok；返回 `res_id`/`forward_id`，`message_id=0` |
| 未知动作 | — | `failed`/1404 |
| 参数错误 | `get_msg` 非法入参 | `failed`/1400 |
| 错误令牌 | — | HTTP 401 |
| body 分档 | 100KB / 1MB / 2MB | 100KB ok；1MB 与 2MB 抵达业务层（长文本被 `retcode=100` 拒绝） |
| body 分档 | 3MB / 5MB / 8MB / 10MB | 服务端**直接断连**，无结构化错误 → 实际上限约 2–3MB |

所有 `message_id` 均原样回查命中；正负皆有，实现不得假设符号。

### 6.3 本地补充：aiohttp 重定向行为（3.14.3）

- POST 遇 302：默认跟随，方法被改为 GET，当前版本剥离 `Authorization`。
- POST 遇 307/308：默认**完整重发** POST、`Authorization` 与请求体到新
  Location——令牌与评论正文双重外泄。故 HTTP 客户端必须显式
  `allow_redirects=False`，与 ws 侧「拒绝握手重定向」等价。

### 6.4 边界

单实例（1.14.17-node）、单账号、一个测试群、一次授权时段内的快照；多账号、
其他版本、其他部署形态未测。本文结论不替代 U1–U8 的逐项授权联验；U3–U8 除
U5/U6 的新事实外均维持未确认（新事实见 6.5 与第 4.4 节）。

### 6.5 补充实测：含图跨群卡片转发的平台资源下载失败（2026-10-05）

部署态排障中取得的一手证据（arclinux，同一实测实例，账号 3539465478）：

- **现象**：转发测试群（780963243）中一条由**其他群（284840486）**转来的合并
  转发卡片（`message_id=1411134300`，卡片 id `MpbOJ…`，卡片创建约 25 分钟后），
  `forward_group_single_msg` 返回 `{"status":"failed","retcode":100,"wording":
  "HTTP download failed: 400"}`。平台重建该卡片时要下载卡片内部资源，下载被拒。
- **对照**：同账号、同目标群，自建**纯文本**两节点卡片 → `send_forward_msg` ok
  → `forward_group_single_msg` 立即转发 ok。证明动作、认证、网络、目标群普遍
  可用，失败特定于该源卡片。
- **展开源卡片**（`get_forward_msg`）：内部节点 `group_id=284840486`（跨群），
  节点含 `image` 段，地址为 `multimedia.nt.qq.com.cn` 的签名 URL（带 `rkey`）。
- **结论**：跨群转来的**含图**卡片，其内部图片签名 URL 在平台侧下载时会失败
  （HTTP 400）；距卡片创建约 25 分钟即可发生，精确时效窗口未测定。纯文本卡片
  不触发下载，不受影响。插件侧处理符合契约：`retcode=100` 属明确业务失败，
  按「明确未发送」登记 `failed`、不重试、不换动作。
- **可观测性缺口（当前版本）**：业务失败只进固定措辞，平台 `wording` 未写入
  日志，首次排障只能靠探针复现定位；补日志的方案已记录待办，未实施前运维可
  用本节的复现方法（对同一 message_id 直接调 `forward_group_single_msg`）确认。

### 6.6 替代通道对比实测：三条卡片投递路径对这一条消息的结果（2026-10-05）

对**同一条失败消息**（`1411134300`，跨群含图卡片）在同一测试群依次测试：

| 通道 | 结果 | 目标群实际展示 |
| --- | --- | --- |
| `forward_group_single_msg`（源消息标识，平台重建） | `failed` / `retcode=100` / “HTTP download failed: 400” | 未发出 |
| `send_msg` 引**源卡片 res_id** 发 forward 段 | 信封 `ok`（`message_id=-532364287`），四项回执核验**全过**（`message_seq=15359`） | **降级空壳**：“#000000 / 34 / 聊天记录 / function(){[native code]} / 查看转发消息”——内容不可用 |
| `upload_forward_msg` 上传展开节点 | `failed` / `retcode=100` / “private image target uid is missing” | 未发出 |
| `send_group_forward_msg` 投递**展开节点**（`sender` 拍平为标量 uin、`name` 取 card/nickname、`content` 为源 message 段数组） | `ok`（`message_id=-1489176197`，新 `res_id=tUiq8YOK…`） | **内容完整**（转发链与图片正常） |

两条关键推论：

1. **假成功陷阱**：`send_msg` 直引旧 res_id 会「发送成功但内容不可用」——回执四项
   （message_id/群/账号/正整数 seq）全部通过，回执层无法发现卡片内容降级。因此
   该通道**不可**作为卡片路径；也说明现有四项回执核验对「旧资源引用」类投递不
   充分。
2. **可用替代**：`send_group_forward_msg` 走「节点重传」路径——平台按目标群上下文
   重新上传卡片内图片，不经旧签名 URL 下载，因此穿过失败。代价是插件必须先
   `get_forward_msg` 展开节点并重组自定义卡片（与现行「卡片不提取节点、不重建」
   的契约相反，且 `upload_forward_msg` 单跳路径因缺图片目标上下文不可用）。
   产品是否改用该路径需专项决策（触及主规格「卡片消息转发」的定义），不在排障
   范围内变更。

### 6.7 与本变更的关系

本节 6.5/6.6 的实测催生变更 `card-forward-node-rebuild`：卡片路径由「按源消息标识
重建」（`forward_group_single_msg`）改为 `get_forward_msg` 展开查询 +
`send_group_forward_msg` 节点重组一步投递，并禁止 `send_msg` 直引旧 `res_id`
（假成功通道）。变更工件见 `openspec/changes/card-forward-node-rebuild/`；本变更
实现后归档，归档件路径以 openspec 实际归档目录为准（本节撰写时该变更尚未归档，
不以此宣称已归档）。6.7 之前的实测记录保持原貌，作为
该决策的证据留存。

### 6.8 节点发送者字段实测：`sender` 被静默丢弃，`uin`/`user_id` 保留原发送者（2026-10-06）

动机：部署态真实投递的卡片在目标群显示为 **bot 本人**发送（用户可见「bot」署名），
而源卡片真人发送者身份丢失。对同一运行实例（SnowLuma 1.14.17-node 容器，账号
3539465478）做发送者字段对照实测；依据的源消息为测试群中一条真人转来的合并转发
卡片（`message_id=-833074616`，原卡片由 uin 1094950020「潜艇二号机」创建，含
video + text 两节点），投递目标为授权测试群（780963243）。

**字段矩阵**（每张卡片一个文本节点，发送后用 `get_msg` 取新卡片 id 再
`get_forward_msg` 展开，逐节点回查 `sender`，全部 `status=ok`）：

| 节点 `data` 字段 | 回查节点 `sender.user_id` | 节点昵称 |
| --- | --- | --- |
| `sender`（标量 uin 字符串）+ `name` | **3539465478（bot 自己）** | 潜艇二号机 |
| `uin`（标量 uin 字符串）+ `name` | **1094950020（原发送者）** | 潜艇二号机 |
| `user_id`（整数）+ `nickname` | **1094950020（原发送者）** | 潜艇二号机 |
| 无发送者字段 | 3539465478（bot 自己） | `3539465478`（uin 字符串回退） |

**真实内容复验**：以 `uin` + `name` 投递原卡片全部两个节点（video 段由平台按
目标群上下文重新上传），`status=ok`；回查展开的每个节点 `sender.user_id` 均为
1094950020、segment 结构与源一致（video / text 各一）。

**源码根因**（GitHub `SnowLuma/SnowLuma` main，发布版本 v1.14.21）：
`packages/onebot/src/modules/message-actions.ts` 的 `parseForwardNodes` 自定义
节点分支只读 `nodeData.user_id ?? nodeData.uin`（缺省 `|| ref.selfId`，即登录账号
自身）与 `nodeData.nickname ?? nodeData.name`；`sender` 不在解析范围。
`assertForwardNodeMetadata` 只要求未知字段为标量（否则 `retcode=1400`），标量
`sender` 因此**通过校验但被静默丢弃**——这正是 6.6 中「sender 对象形态报 1400、
标量通过」的真相：1400 来自标量校验而非 sender 被接受。bridge 层
`packages/core/src/bridge/apis/forward.ts` 的 `buildForwardPushBody` 以节点
`userUin` 写 `responseHead.fromUin`、`nickname` 写 `forward.friendName`，即 wire
上每条节点的发送者身份完全由 `uin`/`user_id` 决定。

**结论与影响**：

1. 卡片路径的节点发送者字段必须是 `uin`（或 `user_id`）、昵称 `name`（或
   `nickname`）。已修订 `send_material.build_card_nodes`（`sender` → `uin`）并同步
   主规格、文档与测试；文字路径 `snowluma_client._text_card_node` 本就使用
   `uin`/`name`，不受影响。
2. **普通图片路径无法保留原发送者**：`send_msg` 段直发的产物发送者必然为 bot
   （协议层面即当前登录账号）。实测把同一条 image+text 源消息改走
   `send_forward_msg` 单节点卡片（`uin`/`name` + 源段原样）可保留原发送者
   （回查节点 sender 为源发送者、图片段被平台接受），但普通消息的展示形态由
   「消息」变为「单节点聊天记录」，属产品语义变更，触及主规格，未在本次修订内。
3. 头像由客户端按 `uin` 渲染：uin 正确传递后，若原发送人是目标群成员/账号好友
   即显示其头像，否则显示默认头像；本机侧只能证明 uin 与昵称写入正确，头像的
   最终展示需在目标群目视确认（实测群中各变体卡片已按 A/B/C/D/E 标注供人工核对）。
4. 本节结论限于该实例（1.14.17-node）、单账号、该测试群的一次快照，不替代
   真实 QQ 联验清单（`docs/real-qq-validation.md`）的逐项确认。
