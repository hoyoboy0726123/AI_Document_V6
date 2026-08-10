import { memo, useCallback, useEffect, useRef, useState } from "react";
import { Button, Input, Tag, Tooltip } from "antd";
import { SendOutlined, StopOutlined, SettingOutlined } from "@ant-design/icons";
import apiClient from "../../services/api";

const { TextArea } = Input;

/**
 * 合併後的單一輸入框（V6）。
 *
 * 取代原本「左側＝新問題、右下＝追問」的兩個框。判斷交給後端的
 * /rag/followup-check（純規則、不呼叫 LLM），結果顯示成一個可以按掉的標籤。
 *
 * 為什麼標籤一定要能按掉：任何自動判斷都會有漏網。實測「冷凝測試方法與條件」
 * 曾被誤判成追問而繼承上一輪的「濕度」去查。使用者要能在送出前就看到並否決，
 * 而不是送出後才發現查錯對象。
 *
 * 自持輸入狀態（不把 value 提到父層）：串流生成時父層每個 token 都重繪，
 * 打字會嚴重延遲 —— 這是 V5 already 修過的問題，不要退回去。
 */
const Composer = memo(function Composer({
  loading,
  qaMode,
  onSubmit,
  onStop,
  onOpenSettings,
  scopeLabel,
  hasHistory,
  historyForCheck,
}) {
  const [value, setValue] = useState("");
  const [check, setCheck] = useState(null);   // { is_followup, inherited, reason }
  // null = 照系統判斷；"followup" / "new" = 使用者手動指定。
  // 必須是三態而不是布林：系統也可能把追問誤判成新問題
  //（實測「冷凝測試方法與條件」），那時要能反向強制延續。
  const [override, setOverride] = useState(null);
  const timerRef = useRef(null);
  const seqRef = useRef(0);

  // 打字時去抖動查詢判斷結果。300ms 是「停下來想一下」的長度，
  // 比這短會在連續輸入時打出一堆無用請求。
  useEffect(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    const text = value.trim();
    if (!text || !hasHistory) { setCheck(null); return; }
    timerRef.current = setTimeout(async () => {
      const seq = ++seqRef.current;
      try {
        const res = await apiClient.post("rag/followup-check", {
          question: text,
          conversation_history: historyForCheck,
        });
        // 慢回的舊請求不可以蓋掉新結果
        if (seq === seqRef.current) setCheck(res.data);
      } catch { /* 判斷失敗就不顯示標籤，不影響送出 */ }
    }, 300);
    return () => { if (timerRef.current) clearTimeout(timerRef.current); };
  }, [value, hasHistory, historyForCheck]);

  // 送出時實際採用的判定：使用者指定優先，否則照系統判斷。
  const effectiveFollowup = override
    ? override === "followup"
    : Boolean(check?.is_followup);

  const submit = useCallback(() => {
    const text = value.trim();
    if (!text || loading) return;
    onSubmit(text, { isFollowup: effectiveFollowup });
    setValue("");
    setCheck(null);
    setOverride(null);
  }, [value, loading, effectiveFollowup, onSubmit]);

  // 有歷史且已經打了字就一定要顯示狀態。原本只在判定為「延續」時顯示，
  // 判定為「新問題」是靜默的 —— 但靜默有歧義：使用者分不出
  // 「系統判斷是新問題」和「系統根本沒判斷」。
  const showState = hasHistory && value.trim() && check;

  return (
    <div className="composer">
      <div className="composer-ctx">
        {/* 模式選擇移進設定抽屜。混合是預設且幾乎總是對的（路由是確定性規則，
            微秒級），三選一對使用者只是多餘的認知負擔。
            但「被鎖在非預設模式」必須看得見 —— 否則使用者會納悶為什麼每次都要
            十幾秒（Agent），卻找不到原因。預設模式安靜，非預設才出聲。 */}
        {qaMode !== "hybrid" && (
          <Tooltip title="已手動鎖定查詢模式。點一下開啟設定改回「混合（自動）」。">
            <Tag
              className="composer-tag composer-state"
              color="purple"
              bordered={false}
              onClick={onOpenSettings}
            >
              已鎖定：{qaMode === "agent" ? "Agent" : "純 RAG"}
            </Tag>
          </Tooltip>
        )}
        {scopeLabel && (
          <Tag className="composer-tag" color="blue" bordered={false}>
            範圍：{scopeLabel}
          </Tag>
        )}
        {showState && (
          <Tooltip
            title={
              <span>
                {override ? "你手動指定的" : check.reason}
                <br />
                點一下切換成「{effectiveFollowup ? "獨立的新問題" : `延續：${check.inherited || "上一題"}`}」
              </span>
            }
          >
            <Tag
              className="composer-tag composer-state"
              color={effectiveFollowup ? "orange" : "default"}
              bordered={false}
              // 兩種狀態都可以點著切換。單向的「✕ 取消延續」不夠 ——
              // 系統把追問誤判成新問題時，使用者同樣需要能改回來。
              onClick={() => setOverride(effectiveFollowup ? "new" : "followup")}
            >
              {effectiveFollowup
                ? `延續：${check.inherited || "上一題"}`
                : "新問題"}
              {override && " ·  已手動指定"}
            </Tag>
          </Tooltip>
        )}
        <span className="composer-spacer" />
        <Tooltip title="查詢設定">
          <Button size="small" type="text" icon={<SettingOutlined />} onClick={onOpenSettings} />
        </Tooltip>
      </div>

      <div className="composer-box">
        <TextArea
          value={value}
          onChange={(e) => {
            setValue(e.target.value);
            // 問題改了，先前的手動指定就不該繼續套用 —— 使用者可能已經
            // 把「那濕度呢」改成一個完整的新問題了。
            if (override) setOverride(null);
          }}
          placeholder={hasHistory ? "接著問，或直接問新問題…" : "輸入問題…"}
          autoSize={{ minRows: 1, maxRows: 8 }}
          variant="borderless"
          onKeyDown={(e) => {
            // Enter 送出、Shift+Enter 換行。輸入法組字中（isComposing）不可送出，
            // 否則中文選字按 Enter 會直接把半成品送出去。
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              submit();
            }
          }}
        />
        {loading ? (
          <Button danger icon={<StopOutlined />} onClick={onStop} shape="circle" />
        ) : (
          <Button
            type="primary"
            icon={<SendOutlined />}
            onClick={submit}
            disabled={!value.trim()}
            shape="circle"
          />
        )}
      </div>
      <div className="composer-hint">
        Enter 送出 · Shift+Enter 換行
        {hasHistory
          ? " · 系統會自動判斷這是追問還是新問題，點標籤可改"
          : " · 這是本對話的第一題"}
      </div>
    </div>
  );
});

export default Composer;
