import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AutoComplete,
  Button,
  Card,
  Col,
  Empty,
  InputNumber,
  Row,
  Select,
  Space,
  Spin,
  Statistic,
  Tag,
  Typography,
  message,
} from "antd";
import { NodeIndexOutlined, ReloadOutlined } from "@ant-design/icons";
import ForceGraph2D from "react-force-graph-2d";
import AppLayout from "../components/Layout/AppLayout";
import apiClient from "../services/api";

const { Text } = Typography;

// Distinct color per spec family
const TYPE_COLORS = {
  // 通用結構節點
  document: "#000000",
  section: "#8c8c8c",
  // 規範家族
  ISO: "#1677ff",
  IEC: "#13c2c2",
  MIL_STD: "#fa541c",
  MIL_HDBK: "#d4380d",
  MIL_DTL: "#ad2102",
  MIL_PRF: "#871400",
  ASTM: "#722ed1",
  IEEE: "#eb2f96",
  SAE: "#fa8c16",
  JIS: "#52c41a",
  CNS: "#a0d911",
  EN: "#2f54eb",
  UL: "#faad14",
};

const REL_COLORS = {
  // 通用結構關係
  contains: "#bfbfbf",
  part_of: "#d9d9d9",
  // 語意關係
  references: "#1677ff",
  supersedes: "#fa541c",
  defines: "#52c41a",
  requires: "#eb2f96",
  derives_from: "#722ed1",
};

const REL_LABELS = {
  contains: "包含",
  part_of: "屬於",
  references: "引用",
  supersedes: "取代",
  defines: "定義",
  requires: "要求",
  derives_from: "衍生自",
};

const KnowledgeGraphPage = () => {
  const fgRef = useRef();
  const containerRef = useRef();
  const [stats, setStats] = useState({ total_entities: 0, total_relations: 0, type_counts: {}, rel_counts: {} });
  const [searchOptions, setSearchOptions] = useState([]);
  const [searchValue, setSearchValue] = useState("");
  const [selectedCanonicalId, setSelectedCanonicalId] = useState(null);
  const [hops, setHops] = useState(2);
  // 依文件篩選：全庫混畫 41 份文件沒有可讀性，預設仍為全部（不改變既有行為）
  const [docFilter, setDocFilter] = useState(null);
  const [docOptions, setDocOptions] = useState([]);
  const [graphData, setGraphData] = useState({ nodes: [], links: [] });
  const [loading, setLoading] = useState(false);
  const [selectedNode, setSelectedNode] = useState(null);
  const [size, setSize] = useState({ width: 800, height: 600 });

  // Responsive container sizing
  useEffect(() => {
    const update = () => {
      if (containerRef.current) {
        const rect = containerRef.current.getBoundingClientRect();
        setSize({ width: Math.max(400, rect.width), height: Math.max(400, window.innerHeight - 280) });
      }
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, []);

  const loadStats = useCallback(async () => {
    try {
      const resp = await apiClient.get("kg/stats");
      setStats(resp.data || {});
    } catch (e) {
      // silent
    }
  }, []);

  useEffect(() => { loadStats(); }, [loadStats]);

  // Initial: load full-graph slice if no center selected
  const loadGraph = useCallback(async (center) => {
    setLoading(true);
    try {
      const params = { hops, limit: center ? 500 : 200 };
      if (center) params.center = center;
      if (docFilter) params.document_id = docFilter;
      const resp = await apiClient.get("kg/graph", { params });
      const { nodes = [], edges = [] } = resp.data || {};
      setGraphData({
        nodes: nodes.map((n) => ({
          id: n.id,
          canonical_id: n.canonical_id,
          name: n.name,
          type: n.type,
          color: TYPE_COLORS[n.type] || "#888",
        })),
        links: edges.map((e) => ({
          source: e.src_id,
          target: e.dst_id,
          rel_type: e.rel_type,
          confidence: e.confidence,
          color: REL_COLORS[e.rel_type] || "#bbb",
        })),
      });
    } catch (e) {
      message.error("載入圖譜失敗：" + (e.response?.data?.detail || e.message));
    } finally {
      setLoading(false);
    }
  }, [hops, docFilter]);

  useEffect(() => {
    loadGraph(selectedCanonicalId);
  }, [loadGraph, selectedCanonicalId]);

  useEffect(() => {
    apiClient.get("documents/", { params: { page: 1, page_size: 200 } })
      .then((r) => setDocOptions((r.data?.items ?? []).map((d) => ({ value: d.id, label: d.title }))))
      .catch(() => {});
  }, []);

  const handleSearch = async (q) => {
    setSearchValue(q);
    if (!q || q.length < 2) {
      setSearchOptions([]);
      return;
    }
    try {
      const resp = await apiClient.get("kg/entities/search", { params: { q, limit: 10 } });
      const opts = (resp.data || []).map((e) => ({
        value: e.canonical_id,
        label: (
          <Space>
            <Tag color={TYPE_COLORS[e.type] || "default"} style={{ margin: 0 }}>{e.type}</Tag>
            <span>{e.canonical_id}</span>
          </Space>
        ),
      }));
      setSearchOptions(opts);
    } catch (e) {
      // silent
    }
  };

  const handleSelect = (val) => {
    setSelectedCanonicalId(val);
    setSearchValue(val);
  };

  const handleNodeClick = (node) => {
    setSelectedNode(node);
    setSelectedCanonicalId(node.canonical_id);
    setSearchValue(node.canonical_id);
    // zoom to node
    if (fgRef.current && node.x !== undefined && node.y !== undefined) {
      fgRef.current.centerAt(node.x, node.y, 800);
      fgRef.current.zoom(3, 800);
    }
  };

  const reset = () => {
    setSelectedCanonicalId(null);
    setSelectedNode(null);
    setSearchValue("");
    if (fgRef.current) fgRef.current.zoomToFit(800);
  };

  const typeBreakdown = useMemo(() => {
    return Object.entries(stats.type_counts || {})
      .sort((a, b) => b[1] - a[1])
      .slice(0, 8);
  }, [stats]);

  const relBreakdown = useMemo(() => {
    return Object.entries(stats.rel_counts || {}).sort((a, b) => b[1] - a[1]);
  }, [stats]);

  return (
    <AppLayout>
      <Row gutter={16}>
        <Col xs={24} lg={6}>
          <Card title="知識圖譜總覽" size="small" style={{ marginBottom: 12 }}>
            <Space direction="vertical" style={{ width: "100%" }} size={8}>
              <Row gutter={8}>
                <Col span={12}>
                  <Statistic title="實體" value={stats.total_entities ?? 0} />
                </Col>
                <Col span={12}>
                  <Statistic title="關係" value={stats.total_relations ?? 0} />
                </Col>
              </Row>
              {typeBreakdown.length > 0 && (
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>實體類型</Text>
                  <div style={{ marginTop: 4 }}>
                    {typeBreakdown.map(([t, n]) => (
                      <Tag key={t} color={TYPE_COLORS[t] || "default"} style={{ marginBottom: 4 }}>
                        {t} · {n}
                      </Tag>
                    ))}
                  </div>
                </div>
              )}
              {relBreakdown.length > 0 && (
                <div>
                  <Text type="secondary" style={{ fontSize: 12 }}>關係類型</Text>
                  <div style={{ marginTop: 4 }}>
                    {relBreakdown.map(([t, n]) => (
                      <Tag key={t} color={REL_COLORS[t] || "default"} style={{ marginBottom: 4 }}>
                        {REL_LABELS[t] || t} · {n}
                      </Tag>
                    ))}
                  </div>
                </div>
              )}
            </Space>
          </Card>

          <Card title="搜尋規範" size="small" style={{ marginBottom: 12 }}>
            <Space direction="vertical" style={{ width: "100%" }}>
              <AutoComplete
                value={searchValue}
                options={searchOptions}
                onSearch={handleSearch}
                onSelect={handleSelect}
                onChange={(v) => setSearchValue(v)}
                placeholder="輸入規範 ID 或關鍵字..."
                style={{ width: "100%" }}
                allowClear
              />
              <Select
                value={docFilter}
                onChange={setDocFilter}
                options={docOptions}
                placeholder="依文件篩選（預設：全部文件）"
                style={{ width: "100%" }}
                allowClear
                showSearch
                optionFilterProp="label"
              />
              <Space>
                <Text style={{ fontSize: 12 }}>展開層數</Text>
                <InputNumber min={1} max={3} value={hops} onChange={setHops} size="small" />
                <Button size="small" icon={<ReloadOutlined />} onClick={reset}>重置</Button>
              </Space>
            </Space>
          </Card>

          {selectedNode && (
            <Card title="節點詳情" size="small">
              <Space direction="vertical" style={{ width: "100%" }} size={4}>
                <Tag color={TYPE_COLORS[selectedNode.type] || "default"}>{selectedNode.type}</Tag>
                <Text strong>{selectedNode.name || selectedNode.canonical_id}</Text>
                {selectedNode.name !== selectedNode.canonical_id && (
                  <Text type="secondary" style={{ fontSize: 12, wordBreak: "break-all" }}>{selectedNode.canonical_id}</Text>
                )}
                <Button
                  size="small"
                  type="primary"
                  block
                  icon={<NodeIndexOutlined />}
                  onClick={() => { setSelectedCanonicalId(selectedNode.canonical_id); }}
                  style={{ marginTop: 8 }}
                >
                  以此為中心展開
                </Button>
              </Space>
            </Card>
          )}
        </Col>

        <Col xs={24} lg={18}>
          <Card
            title={
              <Space>
                <NodeIndexOutlined />
                <span>規範關係圖</span>
                {selectedCanonicalId && <Tag color="blue">{selectedCanonicalId}</Tag>}
                {graphData.nodes.length > 0 && (
                  <Tag>{graphData.nodes.length} 節點 / {graphData.links.length} 邊</Tag>
                )}
              </Space>
            }
            size="small"
          >
            <div ref={containerRef} style={{ width: "100%", minHeight: 500 }}>
              {loading ? (
                <div style={{ textAlign: "center", padding: 80 }}>
                  <Spin size="large" tip="載入中..." />
                </div>
              ) : graphData.nodes.length === 0 ? (
                <Empty
                  description={
                    stats.total_entities === 0
                      ? "知識圖譜尚為空 — 請先上傳規範文件並等待 KG 抽取完成"
                      : "未找到對應實體 — 試試其他關鍵字"
                  }
                  style={{ padding: 80 }}
                />
              ) : (
                <ForceGraph2D
                  ref={fgRef}
                  graphData={graphData}
                  width={size.width}
                  height={size.height}
                  nodeLabel={(n) => `${n.name || n.canonical_id} (${n.type})\n${n.canonical_id}`}
                  nodeColor={(n) => n.color}
                  nodeRelSize={6}
                  linkColor={(l) => l.color}
                  linkDirectionalArrowLength={4}
                  linkDirectionalArrowRelPos={0.85}
                  linkLabel={(l) => `${REL_LABELS[l.rel_type] || l.rel_type} (${(l.confidence * 100).toFixed(0)}%)`}
                  onNodeClick={handleNodeClick}
                  cooldownTicks={120}
                  nodeCanvasObject={(node, ctx, globalScale) => {
                    // 顯示乾淨名稱（章節標題 / 文件標題 / 標準編號），而非系統內部 canonical_id（doc:uuid#NN）。
                    const raw = node.name || node.canonical_id;
                    const label = raw.length > 24 ? `${raw.slice(0, 23)}…` : raw;
                    const fontSize = 12 / globalScale;
                    ctx.font = `${fontSize}px sans-serif`;
                    ctx.fillStyle = node.color;
                    ctx.beginPath();
                    ctx.arc(node.x, node.y, 5, 0, 2 * Math.PI, false);
                    ctx.fill();
                    if (globalScale > 1.2) {
                      ctx.fillStyle = "#222";
                      ctx.textAlign = "center";
                      ctx.textBaseline = "top";
                      ctx.fillText(label, node.x, node.y + 6);
                    }
                  }}
                />
              )}
            </div>
          </Card>
        </Col>
      </Row>
    </AppLayout>
  );
};

export default KnowledgeGraphPage;
