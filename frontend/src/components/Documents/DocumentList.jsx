
import { useEffect, useRef, useState } from 'react';
import {
  Breadcrumb,
  Button,
  Card,
  Col,
  Form,
  Input,
  Modal,
  Popconfirm,
  Row,
  Select,
  Space,
  Table,
  Tag,
  message,
  Divider,
  List,
  Typography,
} from 'antd';
import { DeleteOutlined, FolderOpenOutlined, FolderOutlined, HomeOutlined, SearchOutlined, DownloadOutlined, UpOutlined } from '@ant-design/icons';
import { useSearchParams } from 'react-router-dom';
import apiClient from '../../services/api';
import PdfPreviewModal from './PdfPreviewModal';

const DEFAULT_PAGE_SIZE = 10;

// Recursively collect a folder's ID and all its descendant IDs
function getDescendantIds(folderId, allFolders) {
  const ids = [folderId];
  allFolders
    .filter((f) => f.parent_id === folderId)
    .forEach((child) => ids.push(...getDescendantIds(child.id, allFolders)));
  return ids;
}

// Build API folder filter params from a folder selection
function buildFolderParam(currentFolderId, allFolders) {
  if (!currentFolderId) return {};
  if (currentFolderId === '__root__') return { folder_id: '__root__' };
  const ids = getDescendantIds(currentFolderId, allFolders);
  return ids.length > 0 ? { folder_ids: ids.join(',') } : {};
}

const KeywordList = ({ keywords }) => {
  const [expanded, setExpanded] = useState(false);

  if (!Array.isArray(keywords) || keywords.length === 0) {
    return '-';
  }

  if (keywords.length <= 3) {
    return (
      <Space wrap>
        {keywords.map((kw) => (
          <Tag key={kw}>{kw}</Tag>
        ))}
      </Space>
    );
  }

  if (expanded) {
    return (
      <Space wrap>
        {keywords.map((kw) => (
          <Tag key={kw}>{kw}</Tag>
        ))}
        <Tag
          icon={<UpOutlined />}
          style={{ cursor: 'pointer', borderStyle: 'dashed' }}
          onClick={(e) => {
            e.stopPropagation();
            setExpanded(false);
          }}
        >
          收起
        </Tag>
      </Space>
    );
  }

  return (
    <Space wrap>
      {keywords.slice(0, 3).map((kw) => (
        <Tag key={kw}>{kw}</Tag>
      ))}
      <Tag
        style={{ cursor: 'pointer', borderStyle: 'dashed' }}
        onClick={(e) => {
          e.stopPropagation();
          setExpanded(true);
        }}
      >
        +{keywords.length - 3}
      </Tag>
    </Space>
  );
};

const DocumentList = ({ onView }) => {
  const [searchParams] = useSearchParams();
  const currentFolderId = searchParams.get('folder_id') ?? null; // null = all

  const [loading, setLoading] = useState(false);
  const [documents, setDocuments] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [filterForm] = Form.useForm();
  const [metadataFields, setMetadataFields] = useState([]);
  const [classifications, setClassifications] = useState([]);
  const [dynamicKeywordOptions, setDynamicKeywordOptions] = useState([]);
  const [dynamicFileTypeOptions, setDynamicFileTypeOptions] = useState([]);
  const [dynamicProjectOptions, setDynamicProjectOptions] = useState([]);
  const [keywordsFilter, setKeywordsFilter] = useState([]);
  const [crossDocSearch, setCrossDocSearch] = useState('');
  const [searching, setSearching] = useState(false);
  const [searchResults, setSearchResults] = useState([]);
  const [showSearchResults, setShowSearchResults] = useState(false);

  // 資料夾相關狀態
  const [folders, setFolders] = useState([]);
  const foldersRef = useRef([]); // always up-to-date without dep-array issues
  const [moveModalOpen, setMoveModalOpen] = useState(false);
  const [movingDoc, setMovingDoc] = useState(null);
  const [movingTargetFolderId, setMovingTargetFolderId] = useState(null);

  // 多選批次操作（分類 / 移到資料夾 / 刪除）
  const [selectedRowKeys, setSelectedRowKeys] = useState([]);
  const [bulkClassifyId, setBulkClassifyId] = useState(undefined);
  const [bulkFolderId, setBulkFolderId] = useState(undefined);
  const [bulkLoading, setBulkLoading] = useState(false);

  // PDF 預覽狀態
  const [pdfPreviewVisible, setPdfPreviewVisible] = useState(false);
  const [previewDocumentId, setPreviewDocumentId] = useState(null);
  const [previewDocumentTitle, setPreviewDocumentTitle] = useState('');
  const [previewInitialPage, setPreviewInitialPage] = useState(1);
  const [previewHighlightKeyword, setPreviewHighlightKeyword] = useState('');

  const fetchDocuments = async (params = {}) => {
    try {
      setLoading(true);
      const filterValues = filterForm.getFieldsValue();
      const folderParam = buildFolderParam(currentFolderId, foldersRef.current);

      const resp = await apiClient.get('documents/', {
        params: {
          page,
          page_size: pageSize,
          search_term: filterValues.search_term || undefined,
          classification_id: filterValues.classification_id || undefined,
          project_id: filterValues.project_id || undefined,
          keywords: keywordsFilter.length > 0 ? keywordsFilter.join(',') : undefined,
          ...folderParam,
          ...params,
        },
      });
      setDocuments(resp.data.items);
      setTotal(resp.data.total);
    } catch (error) {
      message.error(error.response?.data?.detail ?? '載入文件列表失敗');
    } finally {
      setLoading(false);
    }
  };

  const fetchMetadataFields = async () => {
    try {
      const resp = await apiClient.get('metadata-fields');
      setMetadataFields(resp.data);
    } catch (error) {
      console.error('無法載入元數據欄位', error);
    }
  };

  // Extract metadata options from a list of documents and merge into state
  const extractAndMergeOptions = (docs) => {
    const kwSet = new Set();
    const ftSet = new Set();
    const projSet = new Set();
    docs.forEach((doc) => {
      // API serializes metadata_data → "metadata"; handle both just in case
      const meta = doc.metadata ?? doc.metadata_data ?? {};
      const kws = meta.keywords;
      if (Array.isArray(kws)) kws.forEach((kw) => kw && kwSet.add(String(kw)));
      else if (typeof kws === 'string' && kws) kwSet.add(kws);
      if (meta.file_type) ftSet.add(String(meta.file_type));
      if (meta.project_id) projSet.add(String(meta.project_id));
    });
    if (kwSet.size > 0)
      setDynamicKeywordOptions((prev) => {
        const existing = new Set(prev.map((o) => o.value));
        const merged = [...prev, ...[...kwSet].filter((v) => !existing.has(v)).map((v) => ({ label: v, value: v }))];
        return merged.sort((a, b) => a.label.localeCompare(b.label, 'zh-TW'));
      });
    if (ftSet.size > 0)
      setDynamicFileTypeOptions((prev) => {
        const existing = new Set(prev.map((o) => o.value));
        const merged = [...prev, ...[...ftSet].filter((v) => !existing.has(v)).map((v) => ({ label: v, value: v }))];
        return merged.sort((a, b) => a.label.localeCompare(b.label, 'zh-TW'));
      });
    if (projSet.size > 0)
      setDynamicProjectOptions((prev) => {
        const existing = new Set(prev.map((o) => o.value));
        const merged = [...prev, ...[...projSet].filter((v) => !existing.has(v)).map((v) => ({ label: v, value: v }))];
        return merged.sort((a, b) => a.label.localeCompare(b.label, 'zh-TW'));
      });
  };

  // Fetch docs to populate filter options, scoped to current folder if applicable
  const fetchFilterOptions = async (folderParam = {}) => {
    try {
      const resp = await apiClient.get('documents/', {
        params: { page: 1, page_size: 500, ...folderParam },
      });
      const docs = resp.data?.items ?? [];
      extractAndMergeOptions(docs);
    } catch (error) {
      console.error('無法載入篩選選項', error);
    }
  };

  const fetchClassifications = async () => {
    try {
      const resp = await apiClient.get('documents/classifications');
      setClassifications(resp.data ?? []);
    } catch { /* ignore */ }
  };

  const fetchFolders = async () => {
    try {
      const resp = await apiClient.get('folders');
      const data = resp.data ?? [];
      setFolders(data);
      foldersRef.current = data;
    } catch { /* ignore */ }
  };

  useEffect(() => {
    fetchMetadataFields();
    fetchClassifications();
    fetchFolders().then(() => {
      // After folders load, fetch filter options scoped to current folder
      const fp = buildFolderParam(currentFolderId, foldersRef.current);
      fetchFilterOptions(fp);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Also extract options from current-page documents whenever they load
  useEffect(() => {
    if (documents.length > 0) extractAndMergeOptions(documents);
  }, [documents]);

  useEffect(() => {
    fetchDocuments();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, pageSize, keywordsFilter, currentFolderId]);

  // When folder changes: reset keyword options and re-fetch scoped to that folder
  useEffect(() => {
    setDynamicKeywordOptions([]);
    setKeywordsFilter([]);
    const fp = buildFolderParam(currentFolderId, foldersRef.current);
    fetchFilterOptions(fp);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentFolderId]);

  const handleSearch = () => {
    setPage(1);
    fetchDocuments({ page: 1 });
  };

  const handleReset = () => {
    filterForm.resetFields();
    setKeywordsFilter([]);
    setPage(1);
    fetchDocuments({
      page: 1,
      search_term: undefined,
      classification_id: undefined,
      file_type: undefined,
      project_id: undefined,
      keywords: undefined,
    });
  };

  const handleDelete = async (documentId) => {
    try {
      await apiClient.delete(`documents/${documentId}`);
      message.success('文件已刪除');
      fetchDocuments();
      fetchFilterOptions();
    } catch (error) {
      message.error(error.response?.data?.detail ?? '刪除文件失敗');
    }
  };

  const handleCrossDocSearch = async () => {
    const query = crossDocSearch.trim();
    if (!query) {
      message.warning('請輸入搜尋關鍵字');
      return;
    }

    setSearching(true);
    try {
      const filterValues = filterForm.getFieldsValue();

      const folderSearchParam = buildFolderParam(currentFolderId, foldersRef.current);

      const response = await apiClient.get('documents/search-text-all', {
        params: {
          q: query,
          classification_id: filterValues.classification_id,
          file_type: filterValues.file_type,
          project_id: filterValues.project_id,
          ...folderSearchParam,
        },
      });
      setSearchResults(response.data.matches);
      setShowSearchResults(true);
      message.success(
        `找到 ${response.data.total_matches} 個匹配結果（跨 ${response.data.total_documents} 份文件）`
      );
    } catch (error) {
      message.error(error.response?.data?.detail ?? '搜尋失敗');
    } finally {
      setSearching(false);
    }
  };

  const handleClearSearch = () => {
    setCrossDocSearch('');
    setSearchResults([]);
    setShowSearchResults(false);
  };

  const handleSearchResultClick = (item) => {
    // 直接在當前頁面打開 PDF 預覽，而不是跳轉到詳情頁
    const keyword = crossDocSearch.trim();
    console.log('點擊搜索結果:', {
      documentId: item.document_id,
      title: item.document_title,
      page: item.page,
      keyword: keyword
    });

    setPreviewDocumentId(item.document_id);
    setPreviewDocumentTitle(item.document_title);
    setPreviewInitialPage(item.page);
    setPreviewHighlightKeyword(keyword);
    setPdfPreviewVisible(true);
  };

  const handleClosePdfPreview = () => {
    setPdfPreviewVisible(false);
    // 保留搜索結果，用戶可以繼續查看下一個結果
  };

  // Keywords: always use dynamic options (free-form tags, not predefined)
  const keywordOptions = dynamicKeywordOptions;

  // For file_type / project_id: prefer admin-configured options, fall back to dynamic
  const selectOptions = (fieldName) => {
    const adminOpts = metadataFields
      .find((item) => item.name === fieldName)
      ?.options?.map((opt) => ({ label: opt.display_value, value: opt.value })) ?? [];
    if (adminOpts.length > 0) return adminOpts;
    if (fieldName === 'file_type') return dynamicFileTypeOptions;
    if (fieldName === 'project_id') return dynamicProjectOptions;
    return [];
  };

  // 根據欄位名稱和值，查找對應的顯示名稱
  const getDisplayValue = (fieldName, value) => {
    if (!value) return null;
    const field = metadataFields.find((item) => item.name === fieldName);
    if (!field || !field.options) return value;
    const option = field.options.find((opt) => opt.value === value);
    return option ? option.display_value : value;
  };

  // Build breadcrumb path for the current folder
  const buildBreadcrumb = () => {
    if (!currentFolderId || currentFolderId === '__root__') return null;
    const folderMap = {};
    folders.forEach((f) => { folderMap[f.id] = f; });
    const path = [];
    let cur = folderMap[currentFolderId];
    while (cur) {
      path.unshift(cur);
      cur = cur.parent_id ? folderMap[cur.parent_id] : null;
    }
    return path;
  };

  const handleMoveDoc = async (record) => {
    setMovingDoc(record);
    setMovingTargetFolderId(record.folder_id ?? null);
    await fetchFolders(); // always get latest folders
    setMoveModalOpen(true);
  };

  const handleMoveConfirm = async () => {
    if (!movingDoc) return;
    try {
      await apiClient.put(`documents/${movingDoc.id}`, {
        folder_id: movingTargetFolderId ?? '__unset__',
      });
      message.success('已移動文件');
      setMoveModalOpen(false);
      fetchDocuments();
    } catch (err) {
      message.error(err.response?.data?.detail ?? '移動失敗');
    }
  };

  // ── 批次操作（對 selectedRowKeys 逐一呼叫既有單筆端點）──────────────────
  const runBulk = async (fn, verb) => {
    const ids = [...selectedRowKeys];
    if (ids.length === 0) return;
    setBulkLoading(true);
    const results = await Promise.allSettled(ids.map(fn));
    const ok = results.filter((r) => r.status === 'fulfilled').length;
    const fail = ids.length - ok;
    setBulkLoading(false);
    if (fail === 0) message.success(`已${verb} ${ok} 份文件`);
    else message.warning(`${verb}完成：成功 ${ok} 份、失敗 ${fail} 份`);
    setSelectedRowKeys([]);
    fetchDocuments();
  };

  const handleBulkDelete = () =>
    runBulk((id) => apiClient.delete(`documents/${id}`), '刪除');

  const handleBulkClassify = () => {
    const cid = bulkClassifyId;
    if (!cid) return;
    setBulkClassifyId(undefined);
    runBulk((id) => apiClient.put(`documents/${id}`, { classification_id: cid }), '套用分類至');
  };

  const handleBulkMove = () => {
    if (bulkFolderId === undefined) return;
    const fid = bulkFolderId || '__unset__';
    setBulkFolderId(undefined);
    runBulk((id) => apiClient.put(`documents/${id}`, { folder_id: fid }), '移動');
  };

  const handleDownload = async (record) => {
    try {
      const response = await apiClient.get(`documents/${record.id}/pdf`, {
        responseType: 'blob',
      });

      // Create a blob link to download
      const url = window.URL.createObjectURL(new Blob([response.data]));
      const link = document.createElement('a');
      link.href = url;

      // Try to get filename from content-disposition header or fallback
      let filename = `${record.title}.pdf`;
      const contentDisposition = response.headers['content-disposition'];
      if (contentDisposition) {
        // Try to match filename*=utf-8''encoded_filename
        const filenameStarMatch = contentDisposition.match(/filename\*=utf-8''([^;]+)/i);
        if (filenameStarMatch && filenameStarMatch.length === 2) {
          filename = decodeURIComponent(filenameStarMatch[1]);
        } else {
          // Fallback to filename="filename"
          const filenameMatch = contentDisposition.match(/filename="?([^"]+)"?/);
          if (filenameMatch && filenameMatch.length === 2) {
            filename = filenameMatch[1];
          }
        }
      }

      link.setAttribute('download', filename);
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(url);
    } catch (error) {
      console.error('Download error:', error);
      message.error('下載失敗');
    }
  };

  const columns = [
    {
      title: '標題',
      dataIndex: 'title',
      render: (text, record) => (
        <Button type="link" onClick={() => onView?.(record)}>
          {text}
        </Button>
      ),
    },
    {
      title: '所屬專案',
      dataIndex: ['metadata', 'project_id'],
      render: (value) => {
        const displayValue = getDisplayValue('project_id', value) || value;
        return displayValue ? <Tag color="purple">{displayValue}</Tag> : '-';
      },
    },
    {
      title: '關鍵字',
      dataIndex: ['metadata', 'keywords'],
      render: (keywords) => <KeywordList keywords={keywords} />,
    },
    {
      title: '分類結果',
      dataIndex: 'classification',
      render: (classification) => {
        if (!classification) return <Tag>尚未分類</Tag>;
        const displayText = classification.code
          ? `${classification.name} (${classification.code})`
          : classification.name;
        return <Tag color="green">{displayText}</Tag>;
      },
    },

    {
      title: '操作',
      key: 'actions',
      render: (_, record) => (
        <Space>
          <Button size="small" onClick={() => onView?.(record)}>
            檢視
          </Button>
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => handleDownload(record)}
          >
            下載
          </Button>
          <Button
            size="small"
            icon={<FolderOutlined />}
            onClick={() => handleMoveDoc(record)}
            title="移至資料夾"
          />
          <Popconfirm
            title="確認刪除"
            description="確定要刪除此文件嗎？此操作將同時刪除相關的向量資料和 PDF 檔案，無法復原。"
            onConfirm={() => handleDelete(record.id)}
            okText="確定"
            cancelText="取消"
            okType="danger"
          >
            <Button size="small" danger icon={<DeleteOutlined />}>
              刪除
            </Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];

  return (
    <Card>
      {/* 路徑麵包屑 */}
      {(() => {
        const path = buildBreadcrumb();
        const items = [
          {
            title: (
              <span style={{ cursor: 'pointer' }}>
                <HomeOutlined /> 全部文件
              </span>
            ),
            href: '/documents',
          },
        ];
        if (currentFolderId === '__root__') {
          items.push({ title: <span><FolderOpenOutlined /> 未歸類</span> });
        } else if (path) {
          path.forEach((f) => {
            items.push({
              title: (
                <span>
                  <FolderOpenOutlined /> {f.name}
                </span>
              ),
            });
          });
        }
        return (
          <div
            style={{
              background: '#f5f5f5',
              border: '1px solid #e8e8e8',
              borderRadius: 4,
              padding: '6px 12px',
              marginBottom: 12,
              display: 'flex',
              alignItems: 'center',
            }}
          >
            <Breadcrumb items={items} style={{ fontSize: 13 }} />
          </div>
        );
      })()}

      {/* 第一行：跨文件全文檢索 + 關鍵字標籤篩選 */}
      <Row gutter={16} style={{ marginBottom: 12 }}>
        <Col xs={24} md={12}>
          <Space.Compact style={{ width: '100%' }}>
            <Input
              placeholder={(() => {
                if (!currentFolderId || currentFolderId === '__all__') return '跨文件全文檢索...';
                if (currentFolderId === '__root__') return '搜尋未歸類文件...';
                const name = folders.find((f) => f.id === currentFolderId)?.name;
                return name ? `搜尋「${name}」資料夾內的文件（含子資料夾）...` : '搜尋當前資料夾...';
              })()}
              value={crossDocSearch}
              onChange={(e) => setCrossDocSearch(e.target.value)}
              onPressEnter={handleCrossDocSearch}
              prefix={<SearchOutlined />}
              allowClear
            />
            <Button type="primary" loading={searching} onClick={handleCrossDocSearch}>
              搜尋
            </Button>
            {showSearchResults && (
              <Button onClick={handleClearSearch}>清除結果</Button>
            )}
          </Space.Compact>
        </Col>
        <Col xs={24} md={12}>
          <Select
            mode="multiple"
            allowClear
            placeholder="關鍵字標籤篩選"
            style={{ width: '100%' }}
            options={keywordOptions}
            showSearch
            optionFilterProp="label"
            value={keywordsFilter}
            onChange={(val) => {
              setKeywordsFilter(val);
              setPage(1);
            }}
          />
        </Col>
      </Row>

      {/* 搜尋結果顯示 */}
      {showSearchResults && searchResults.length > 0 && (
        <Card
          size="small"
          title={`搜尋結果 (${searchResults.length} 筆)`}
          style={{ marginBottom: 16 }}
          styles={{ body: { maxHeight: '300px', overflowY: 'auto' } }}
        >
          <List
            size="small"
            dataSource={searchResults}
            renderItem={(item) => (
              <List.Item
                style={{ cursor: 'pointer' }}
                onClick={() => handleSearchResultClick(item)}
              >
                <List.Item.Meta
                  title={
                    <Space>
                      <Typography.Text strong>{item.document_title}</Typography.Text>
                      <Tag color="blue">第 {item.page} 頁</Tag>
                    </Space>
                  }
                  description={
                    <Typography.Text type="secondary">
                      {item.snippet}
                    </Typography.Text>
                  }
                />
              </List.Item>
            )}
          />
        </Card>
      )}

      {showSearchResults && searchResults.length === 0 && (
        <Card size="small" style={{ marginBottom: 16 }}>
          <Typography.Text type="secondary">未找到匹配的結果</Typography.Text>
        </Card>
      )}

      <Divider style={{ margin: '16px 0' }} />

      <Form form={filterForm} layout="inline" onFinish={handleSearch} style={{ marginBottom: 16, flexWrap: 'wrap', gap: '8px 0' }}>
        <Form.Item name="search_term" label="標題" style={{ marginBottom: 0 }}>
          <Input placeholder="輸入標題關鍵字" allowClear style={{ width: 160 }} />
        </Form.Item>
        <Form.Item name="classification_id" label="分類" style={{ marginBottom: 0 }}>
          <Select
            placeholder="選擇分類"
            allowClear
            style={{ width: 180 }}
            options={classifications.map((c) => ({
              label: c.code ? `${c.name} (${c.code})` : c.name,
              value: c.id,
            }))}
          />
        </Form.Item>
        <Form.Item name="project_id" label="專案" style={{ marginBottom: 0 }}>
          <Select placeholder="選擇專案" options={selectOptions('project_id')} allowClear style={{ width: 160 }} />
        </Form.Item>
        <Form.Item style={{ marginBottom: 0 }}>
          <Space>
            <Button type="primary" htmlType="submit">搜尋</Button>
            <Button onClick={handleReset}>重設</Button>
          </Space>
        </Form.Item>
      </Form>

      {selectedRowKeys.length > 0 && (
        <Space
          wrap
          style={{ marginBottom: 12, padding: '8px 12px', background: '#eef5ff', border: '1px solid #cfe0ff', borderRadius: 6 }}
        >
          <Typography.Text strong>已選 {selectedRowKeys.length} 項</Typography.Text>
          <Divider type="vertical" />
          <Select
            placeholder="選擇分類"
            style={{ width: 180 }}
            value={bulkClassifyId}
            onChange={setBulkClassifyId}
            options={classifications.map((c) => ({ label: c.name, value: c.id }))}
            showSearch
            optionFilterProp="label"
            allowClear
          />
          <Button onClick={handleBulkClassify} disabled={!bulkClassifyId} loading={bulkLoading}>
            套用分類
          </Button>
          <Divider type="vertical" />
          <Select
            placeholder="移到資料夾"
            style={{ width: 180 }}
            value={bulkFolderId}
            onChange={setBulkFolderId}
            options={folders.map((f) => ({ label: f.name, value: f.id }))}
            showSearch
            optionFilterProp="label"
            allowClear
          />
          <Button onClick={handleBulkMove} disabled={bulkFolderId === undefined} loading={bulkLoading}>
            移動
          </Button>
          <Divider type="vertical" />
          <Popconfirm
            title="批次刪除"
            description={`確定刪除選取的 ${selectedRowKeys.length} 份文件？將一併刪除向量與 PDF 檔案，無法復原。`}
            okText="刪除"
            okType="danger"
            cancelText="取消"
            onConfirm={handleBulkDelete}
          >
            <Button danger icon={<DeleteOutlined />} loading={bulkLoading}>
              批次刪除
            </Button>
          </Popconfirm>
          <Button type="text" onClick={() => setSelectedRowKeys([])}>
            取消選取
          </Button>
        </Space>
      )}

      <Table
        rowKey="id"
        dataSource={documents}
        columns={columns}
        loading={loading}
        rowSelection={{
          selectedRowKeys,
          onChange: (keys) => setSelectedRowKeys(keys),
        }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          onChange: (newPage, newSize) => {
            setPage(newPage);
            setPageSize(newSize);
          },
        }}
      />

      {/* 移至資料夾 Modal */}
      <Modal
        title={`移動「${movingDoc?.title ?? ''}」至資料夾`}
        open={moveModalOpen}
        onOk={handleMoveConfirm}
        onCancel={() => setMoveModalOpen(false)}
        okText="移動"
        cancelText="取消"
      >
        <Select
          style={{ width: '100%' }}
          allowClear
          placeholder="選擇目標資料夾（清除表示移出所有資料夾）"
          value={movingTargetFolderId}
          onChange={setMovingTargetFolderId}
          options={[
            ...folders.map((f) => ({ label: f.name, value: f.id })),
          ]}
          showSearch
          optionFilterProp="label"
        />
      </Modal>

      {/* PDF 預覽 Modal */}
      <PdfPreviewModal
        open={pdfPreviewVisible}
        documentId={previewDocumentId}
        title={previewDocumentTitle}
        initialPage={previewInitialPage}
        initialHighlightKeyword={previewHighlightKeyword}
        onClose={handleClosePdfPreview}
      />
    </Card>
  );
};

export default DocumentList;

