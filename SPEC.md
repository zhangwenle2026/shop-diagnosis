# 商家诊断工具 · 项目规范 SPEC.md

> 最后更新：2026-05-07
> 负责人：阿奇

---

## 🎯 产品定义

**工具名**：Store Health Diagnosis（商家健康诊断）  
**部署**：NoCode（网页 + 手机 APP）  
**用户**：BD（业务拓展人员）  
**目标**：BD 输入任意商家 ID，快速生成该商家的健康诊断报告

---

## 🖥️ 页面结构

### 顶部输入区
- 商家 ID 输入框
- 日期范围选择（默认：本月）
- 语言切换：中文 / English / Português（默认：Português）
- 按钮：诊断（Diagnosticar / Generate Report / 生成报告）

### 报告区块（按顺序）

1. **Store Information** — 商家基本信息
   - Store ID、Store Name、City、Cuisine Type、数据日期范围

2. **Overall Health Score** — 综合健康分
   - 圆形仪表盘，0-100 分
   - 评级：Excellent(90+) / Good(75-89) / Fair(60-74) / Poor(<60)

3. **GMV Attribution Waterfall** — GMV 归因瀑布图
   - 漏斗：Exposure → Entry Rate → Order Rate → Complete Rate → GMV
   - 每步显示转化率 + 绝对值

4. **Metrics Scorecard** — 指标评分表
   - 列：指标名 / 实际值(Actual) / 行业基准(Benchmark) / 得分(Score)
   - 指标列表见下方「数据字段」

5. **Health Radar** — 雷达图
   - 维度：AOV / Entry Rate / Order Rate / SPU w/Image / New User % / Repurchase Rate / Avg Rating

6. **Diagnosis & Action Plan** — 诊断与行动计划
   - Overall Assessment（总评）
   - Areas Needing Improvement（红色，待改进）
   - Strong Performance（绿色，表现优秀）

---

## 📊 数据字段规范

### ⛔ 铁律：禁止任何模拟数据或虚假数据

**所有字段必须来自真实数据源，未能获取的字段显示「--」，不得填写任何估算值或虚构值。**

### 数据来源优先级

| 字段 | 数据来源 | Skill |
|---|---|---|
| 商家名称、城市、餐厅类型 | DC API / BI 看板 | keeta-data-query-for-front-line |
| 订单量、GMV | DC API | keeta-data-query-for-front-line |
| 有效营业率（Active Rate） | DC API | keeta-data-query-for-front-line |
| 取消率（Cancel Rate） | DC API | keeta-data-query-for-front-line |
| CVR（Visit/Order/Complete） | BI 看板 | bi-query-dashboard-overseas |
| AOV（平均客单价） | BI 看板 / SQL | bi-query-sql |
| 新用户率（New User %） | BI 看板 / SQL | bi-query-sql |
| 复购率（Repurchase Rate） | BI 看板 / SQL | bi-query-sql |
| 商家评分（Avg Rating） | DC API / DB | keeta-brazil-db-ref |
| SPU 数量 / 有图 SPU 比例 | DB | keeta-brazil-db-ref |
| Deep Discount 参与情况 | BI 看板 300001446 | bi-query-dashboard-overseas |

### 指标 Scorecard 完整列表

| 指标 | 英文 | 葡文 |
|---|---|---|
| 平均客单价 | AOV | Ticket Médio |
| 访问转化率 | Visit CVR | Taxa de Visita |
| 下单转化率 | Order CVR | Taxa de Pedido |
| 完成率 | Complete CVR | Taxa de Conclusão |
| 营业时长 | Open Hours | Horas de Operação |
| 新用户率 | New User % | % Novos Usuários |
| 复购率 | Repurchase Rate | Taxa de Recompra |
| 商家评分 | Avg Rating | Avaliação Média |
| 店铺分 | Shop Score | Pontuação da Loja |
| 用户取消率 | Pay Cancel | Cancelamento Pago |
| 在售 SPU 数 | Active SPUs | SPUs Ativos |
| 平均图片数 | Avg Image | Imagens Médias |
| 有图 SPU 比例 | SPU w/ Image | SPU c/ Imagem |

---

## 🔌 技术架构

```
BD（手机/网页）
    ↓ 输入商家ID + 日期 + 语言
NoCode 前端
    ↓ HTTP POST /diagnose?shop_id=xxx&start=xxx&end=xxx
阿奇 API（FastAPI, port 8765）
    ↓ 调用多个 Skill 取真实数据
    ↓ keeta-data-query / bi-query-sql / keeta-brazil-db-ref 等
    ↓ 计算健康分 + 生成诊断结论
    ↓ 返回 JSON
NoCode 前端 渲染报告
```

### API 接口规范

**POST /diagnose**

Request:
```json
{
  "shop_id": "123456",
  "start_date": "2026-05-01",
  "end_date": "2026-05-07",
  "lang": "pt"  // pt / en / zh
}
```

Response:
```json
{
  "store_info": { "id": "...", "name": "...", "city": "...", "cuisine": "..." },
  "health_score": 78,
  "health_grade": "Good",
  "metrics": [ { "key": "aov", "actual": 38.5, "benchmark": 35.0, "score": 85 } ],
  "waterfall": { "exposure": 17000, "entry_rate": 0.248, ... },
  "radar": { "aov": 85, "entry_rate": 70, ... },
  "diagnosis": {
    "overall": "...",
    "improvements": ["Order CVR 低于基准 15%", "..."],
    "strengths": ["Complete CVR 93.3%，高于平均", "..."]
  },
  "data_date": "2026-05-01 ~ 2026-05-07",
  "generated_at": "2026-05-07T01:39:00"
}
```

---

## 🌐 三语规范

| UI 元素 | 中文 | English | Português |
|---|---|---|---|
| 按钮 | 生成报告 | Generate Report | Diagnosticar |
| 健康分标题 | 综合健康分 | Overall Health Score | Pontuação de Saúde |
| 诊断与建议 | 诊断与行动计划 | Diagnosis & Action Plan | Diagnóstico e Plano de Ação |
| 待改进 | 待改进项 | Areas Needing Improvement | Áreas de Melhoria |
| 表现优秀 | 强项 | Strong Performance | Pontos Fortes |

**切换语言时**：页面所有文字实时切换，数据不重新加载。

---

## ⛔ 开发铁律

1. **禁止模拟数据**：任何字段不得使用 hardcode 的假数据，无数据显示 `--`
2. **禁止缓存过期数据**：每次点「诊断」必须重新拉取最新数据
3. **数据来源透明**：报告底部注明数据来源和生成时间
4. **错误处理**：商家 ID 不存在 → 明确提示「商家不存在」，不显示空报告
5. **移动端优先**：布局先适配手机，再适配桌面

---

## 📅 开发计划

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | SPEC 确认 + 数据字段梳理 | ✅ 完成 |
| Phase 2 | 后端 API 接入真实数据源 | 🔄 待开发 |
| Phase 3 | 前端页面（NoCode）三语版 | ⏳ 待开发 |
| Phase 4 | 健康分算法 + 诊断逻辑 | ⏳ 待开发 |
| Phase 5 | 端到端测试 + 上线 | ⏳ 待开发 |
