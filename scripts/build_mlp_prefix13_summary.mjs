/**
 * Build an auditable workbook for the verified native MLP communication-prefix
 * validation.  It deliberately excludes the failed first eight-node prewarm
 * attempt and labels this as a 13-event prefix, not full MLP completion.
 */
import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const repositoryRoot = path.resolve(import.meta.dirname, "..");
const outputDirectory = path.join(
  repositoryRoot,
  "results",
  "mlp-sequential-prefix13-summary-20260730",
);

const sources = [
  {
    nodes: 1,
    directory: path.join(
      repositoryRoot,
      "results",
      "mlp-sequential-prefix13-smoke-20260730",
      "nodes1",
      "rep-1",
    ),
  },
  {
    nodes: 2,
    directory: path.join(
      repositoryRoot,
      "results",
      "mlp-sequential-prefix13-nodes2-4-8-smoke-20260730",
      "nodes2",
      "rep-1",
    ),
  },
  {
    nodes: 4,
    directory: path.join(
      repositoryRoot,
      "results",
      "mlp-sequential-prefix13-nodes2-4-8-smoke-20260730",
      "nodes4",
      "rep-1",
    ),
  },
  {
    nodes: 8,
    directory: path.join(
      repositoryRoot,
      "results",
      "mlp-sequential-prefix13-node8-retry2-20260730",
      "nodes8",
      "rep-1",
    ),
  },
];

function metricFromLine(line) {
  const marker = "pipe-metric: ";
  const position = line.indexOf(marker);
  return position < 0 ? null : JSON.parse(line.slice(position + marker.length));
}

async function loadSource(source) {
  const result = JSON.parse(
    await fs.readFile(path.join(source.directory, "result.json"), "utf8"),
  );
  const files = await fs.readdir(source.directory);
  const metrics = [];
  for (const file of files.filter((name) => name.startsWith("transport-") && name.endsWith(".log"))) {
    const content = await fs.readFile(path.join(source.directory, file), "utf8");
    for (const line of content.split(/\r?\n/)) {
      const metric = metricFromLine(line);
      if (metric) metrics.push({ ...metric, sourceLog: file });
    }
  }
  if (result.status !== "communication-epoch-complete" || metrics.length !== 13) {
    throw new Error(`invalid verified source for ${source.nodes} nodes`);
  }
  return { ...source, result, metrics };
}

const loaded = await Promise.all(sources.map(loadSource));
const rawRows = loaded.flatMap((source) => source.metrics.map((metric) => [
  source.nodes,
  source.result.stack,
  metric.operation,
  metric.bytes,
  metric.cross_node ? 1 : 0,
  metric.elapsed_ns / 1e6,
  metric.synchronization_wait_ns / 1e6,
  metric.sourceLog,
]));

await fs.mkdir(outputDirectory, { recursive: true });
await fs.writeFile(
  path.join(outputDirectory, "mlp_prefix13_summary.csv"),
  [
    "nodes,status,expected_events,completed_events,cross_node_events,total_bytes,cross_node_bytes,prewarm_seconds,epoch_seconds,communication_epoch_wall_seconds",
    ...loaded.map(({ nodes, result, metrics }) => {
      const cross = metrics.filter((metric) => metric.cross_node);
      return [
        nodes,
        result.status,
        result.expected_pipecomm_events,
        result.completed_pipecomm_events,
        cross.length,
        metrics.reduce((sum, metric) => sum + metric.bytes, 0),
        cross.reduce((sum, metric) => sum + metric.bytes, 0),
        result.prewarm_elapsed_seconds,
        result.epoch_completion_seconds,
        result.communication_epoch_wall_seconds,
      ].join(",");
    }),
  ].join("\n") + "\n",
  "utf8",
);

const workbook = Workbook.create();
const summary = workbook.worksheets.add("Summary");
const raw = workbook.worksheets.add("Raw Metrics");
summary.showGridLines = false;
raw.showGridLines = false;

summary.mergeCells("A1:M1");
summary.getRange("A1").values = [["LegoSimbricks 原生 MLP：13 事件通信前缀验证汇总"]];
summary.mergeCells("A2:M2");
summary.getRange("A2").values = [[
  "范围：已验证的固定通信前缀（非完整 18 事件 MLP）。每个节点数仅有 1 次成功运行；不可据此报告均值、置信区间或完整端到端加速比。",
]];
summary.getRange("A4:M4").values = [[
  "节点数", "目标事件", "完成事件", "跨节点事件", "总字节", "跨节点字节", "跨节点字节占比",
  "跨节点读同步等待总和 (ms)", "预热时间 (s)", "事件完成时间 (s)", "通信墙钟时间 (s)", "状态", "证据目录",
]];
summary.getRange("A5:A8").values = [[1], [2], [4], [8]];
summary.getRange("B5:B8").values = loaded.map(({ result }) => [result.expected_pipecomm_events]);
summary.getRange("I5:K8").values = loaded.map(({ result }) => [[
  result.prewarm_elapsed_seconds,
  result.epoch_completion_seconds,
  result.communication_epoch_wall_seconds,
]][0]);
summary.getRange("L5:L8").values = loaded.map(({ result }) => [result.status]);
summary.getRange("M5:M8").values = loaded.map(({ directory }) => [path.relative(repositoryRoot, directory)]);

for (let row = 5; row <= 8; row += 1) {
  summary.getRange(`C${row}`).formulas = [[`=COUNTIF('Raw Metrics'!$A$2:$A$54,A${row})`]];
  summary.getRange(`D${row}`).formulas = [[`=COUNTIFS('Raw Metrics'!$A$2:$A$54,A${row},'Raw Metrics'!$E$2:$E$54,1)`]];
  summary.getRange(`E${row}`).formulas = [[`=SUMIF('Raw Metrics'!$A$2:$A$54,A${row},'Raw Metrics'!$D$2:$D$54)`]];
  summary.getRange(`F${row}`).formulas = [[`=SUMIFS('Raw Metrics'!$D$2:$D$54,'Raw Metrics'!$A$2:$A$54,A${row},'Raw Metrics'!$E$2:$E$54,1)`]];
  summary.getRange(`G${row}`).formulas = [[`=F${row}/E${row}`]];
  summary.getRange(`H${row}`).formulas = [[`=SUMIFS('Raw Metrics'!$G$2:$G$54,'Raw Metrics'!$A$2:$A$54,A${row},'Raw Metrics'!$E$2:$E$54,1,'Raw Metrics'!$C$2:$C$54,"R")`]];
}
summary.getRange("A10:M10").merge();
summary.getRange("A10").values = [[
  "口径：跨节点读同步等待总和为各跨节点 R 操作的 synchronization_wait_ns 之和；并发等待可能重叠，因此它是聚合诊断量，不等同于关键路径同步占比。",
]];

raw.getRange("A1:H1").values = [[
  "节点数", "Swarm stack", "操作", "字节", "跨节点", "耗时 (ms)", "同步等待 (ms)", "来源日志",
]];
raw.getRange(`A2:H${rawRows.length + 1}`).values = rawRows;

for (const sheet of [summary, raw]) {
  sheet.getUsedRange().format.font = { name: "Aptos", size: 10, color: "#1F2937" };
  sheet.getRange("A1:M1").format.fill = "#17365D";
}
summary.getRange("A1:M1").format = {
  fill: "#17365D",
  font: { bold: true, color: "#FFFFFF", size: 16 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
summary.getRange("A2:M2").format = {
  fill: "#EAF2F8",
  font: { italic: true, color: "#1F2937" },
  wrapText: true,
};
summary.getRange("A4:M4").format = {
  fill: "#2F75B5",
  font: { bold: true, color: "#FFFFFF" },
  wrapText: true,
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
summary.getRange("A5:M8").format.borders = { preset: "insideHorizontal", style: "thin", color: "#D9E2F3" };
summary.getRange("A5:M8").format.numberFormat = "0.000";
summary.getRange("A5:F8").format.numberFormat = "#,##0";
summary.getRange("G5:G8").format.numberFormat = "0.0%";
summary.getRange("H5:K8").format.numberFormat = "0.000";
summary.getRange("A10:M10").format = {
  fill: "#FFF2CC",
  font: { italic: true, color: "#7F6000" },
  wrapText: true,
};
summary.getRange("A1:M10").format.autofitColumns();
summary.getRange("A1:M10").format.autofitRows();
summary.getRange("M1:M10").format.columnWidth = 42;
summary.getRange("A2").format.rowHeight = 36;
summary.getRange("A10").format.rowHeight = 34;
summary.freezePanes.freezeRows(4);

raw.getRange("A1:H1").format = {
  fill: "#17365D",
  font: { bold: true, color: "#FFFFFF" },
  horizontalAlignment: "center",
};
raw.getRange(`A2:H${rawRows.length + 1}`).format.borders = { preset: "insideHorizontal", style: "thin", color: "#E5E7EB" };
raw.getRange(`D2:D${rawRows.length + 1}`).format.numberFormat = "#,##0";
raw.getRange(`F2:G${rawRows.length + 1}`).format.numberFormat = "0.000";
raw.getUsedRange().format.autofitColumns();
raw.freezePanes.freezeRows(1);

const inspected = await workbook.inspect({
  kind: "table",
  range: "Summary!A1:M10",
  include: "values,formulas",
  tableMaxRows: 10,
  tableMaxCols: 13,
});
console.log(inspected.ndjson);
const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 50 },
  summary: "formula error scan",
});
console.log(errors.ndjson);
const preview = await workbook.render({ sheetName: "Summary", range: "A1:M10", scale: 1.5, format: "png" });
await fs.writeFile(path.join(outputDirectory, "summary-preview.png"), new Uint8Array(await preview.arrayBuffer()));
const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(path.join(outputDirectory, "mlp_prefix13_summary.xlsx"));
