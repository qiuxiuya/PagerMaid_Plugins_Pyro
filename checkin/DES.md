在全局设定的时间范围内随机发送签到提醒消息。

指令：`,checkin`

# 设置全局签到时间范围
`,checkin start 8:00 end 20:00`

# 添加签到任务
`,checkin add @username 今天也要记得打卡哦`
`,checkin add 123456789 该签到了`

# 可用命令

`,checkin start HH:MM end HH:MM` - 设置全局签到时间范围
`,checkin add <用户名/ID> <消息内容>` - 添加签到任务，每天在全局时间范围内随机时间发送消息
`,checkin rm <任务ID>` - 删除指定任务
`,checkin pause <任务ID>` - 暂停指定任务
`,checkin resume <任务ID>` - 恢复指定任务
`,checkin run <任务ID>` - 立即手动运行指定任务
`,checkin run all` - 立即手动运行所有任务
`,checkin status` - 查看当前时间范围和任务列表
`,checkin h` - 显示帮助
