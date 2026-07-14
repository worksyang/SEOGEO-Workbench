# OCR模型配置说明

## 📋 当前配置（2026-02-02更新）

### 主模型
- **模型名称**: `Qwen/Qwen3-VL-235B-A22B-Instruct`
- **平台**: 硅基流动 (SiliconFlow)
- **API地址**: `https://api.siliconflow.cn/v1`
- **处理模式**: 串行处理（max_workers=1）

### 备用模型1
- **模型名称**: `Gemini-3-Flash`
- **平台**: Poe平台
- **API地址**: `https://api.poe.com/v1`
- **触发条件**: 主模型超时或失败时自动切换

### 备用模型2
- **模型名称**: `gemini-2.5-flash-lite`
- **平台**: ChatNP-Gemini渠道
- **API地址**: `http://doubao.zwchat.cn/v1`
- **触发条件**: 备用模型1失败时自动切换

## 🔧 配置文件位置

### 主配置文件
- **文件**: `SomeURL2MD/openai_concurrent_service.py`
- **配置区域**: `MODEL_CONFIG` 字典（第24-42行）

### API密钥配置
- **文件**: `openaiapi.json`
- **包含**: 所有平台的API密钥和Base URL

## 🚀 使用方式

### 1. 直接运行img.py
```bash
python img.py
```
- 自动使用串行模式（max_workers=1）
- 使用配置的主模型：Qwen/Qwen3-VL-235B-A22B-Instruct

### 2. 测试配置
```bash
python test_ocr_config.py
```
- 验证模型配置是否正确
- 显示当前使用的模型信息

## 📊 处理流程

```
图片输入
  ↓
主模型识别 (Qwen/Qwen3-VL-235B-A22B-Instruct)
  ↓
成功? → 是 → 返回结果
  ↓ 否
备用模型1 (Gemini-3-Flash)
  ↓
成功? → 是 → 返回结果
  ↓ 否
备用模型2 (gemini-2.5-flash-lite)
  ↓
返回结果或错误
```

## ⚙️ 串行 vs 并行处理

### 串行处理（当前配置）
- **max_workers = 1**
- 每次只处理1张图片
- 适合：API有速率限制、需要稳定性
- 速度：较慢，但更稳定

### 并行处理（可选）
- **max_workers > 1**（如10、20、30等）
- 同时处理多张图片
- 适合：API无限制、追求速度
- 速度：快，但可能触发限流

## 🔄 如何切换回并行模式

如果需要切换回并行模式，修改 `img.py` 第539行：

```python
# 串行模式（当前）
ocr_service = OpenAIConcurrentService(max_workers=1, max_retries=3)

# 并行模式（修改为）
ocr_service = OpenAIConcurrentService(max_workers=20, max_retries=3)
```

## 🔑 模型切换

如需切换主模型，修改 `SomeURL2MD/openai_concurrent_service.py` 中的 `MODEL_CONFIG`：

```python
MODEL_CONFIG = {
    "primary_platform": "siliconflow",  # 修改平台
    "primary_model": "Qwen/Qwen3-VL-235B-A22B-Instruct",  # 修改模型
    # ... 其他配置
}
```

## 📝 注意事项

1. **API密钥安全**: 不要将 `openaiapi.json` 提交到公开仓库
2. **速率限制**: 串行模式可以避免触发API速率限制
3. **超时设置**: 每个模型有30秒超时，总计150秒（包含重试）
4. **备用模型**: 自动重试机制确保高成功率

## 🐛 故障排查

### 问题1: 主模型连接失败
- 检查 `openaiapi.json` 中 siliconflow 的配置
- 验证API密钥是否有效
- 检查网络连接

### 问题2: 所有模型都失败
- 查看日志中的错误信息
- 确认所有平台的API密钥都有效
- 检查是否触发了速率限制

### 问题3: 处理速度太慢
- 当前使用串行模式，这是正常的
- 如需提速，可以增加 max_workers 值
- 注意：提速可能导致API限流

## 📞 技术支持

如有问题，请检查：
1. 运行 `test_ocr_config.py` 查看配置状态
2. 查看日志输出中的错误信息
3. 确认所有依赖包已正确安装
