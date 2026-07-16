/**
 * TaskStatusContext
 * 全域追蹤背景任務（pdf_analyze / vectorize / vl_vectorize）的狀態，每 4 秒 poll 一次。
 * 任務 ID 存在 localStorage key: "activeTasks" (JSON array)
 *
 * Audit M 修正：
 * - 任務 completed/failed 後即停止輪詢該任務（顯示狀態保留），不再無限打 API。
 * - 監聽 storage 事件：多分頁同步 tasks 清單（B 分頁能看到 A 分頁新增的任務）。
 * - persist 以 localStorage 現值為基準合併，不再用本分頁可能過時的 state 整包覆寫。
 * - poll 內 JSON.parse 加 try/catch（壞值不再每 4 秒炸一次 unhandled rejection）。
 * - 分頁不可見時暫停輪詢（visibilitychange）。
 */
import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";
import apiClient from "../services/api";
import useAuthStore from "../stores/authStore";

const TaskStatusContext = createContext(null);

const STORAGE_KEY = "activeTasks"; // [{task_id, document_id, document_title, task_type}]
const POLL_INTERVAL = 4000;

const readStored = () => {
  try {
    const v = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? "[]");
    return Array.isArray(v) ? v : [];
  } catch {
    try { localStorage.removeItem(STORAGE_KEY); } catch { /* ignore */ }
    return [];
  }
};

export const TaskStatusProvider = ({ children }) => {
  const [tasks, setTasks] = useState(readStored);
  // taskStatuses: { [task_id]: TaskRead }
  const [taskStatuses, setTaskStatuses] = useState({});
  // onComplete callbacks: { [task_id]: (taskData) => void }
  const callbacksRef = useRef({});
  // 已終結（completed/failed）的任務 → 不再輪詢
  const doneRef = useRef(new Set());

  // 以 localStorage 現值為基準做增/刪，避免多分頁互相用舊 state 覆寫
  const _mutateStored = (mutator) => {
    const next = mutator(readStored());
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(next)); } catch { /* ignore */ }
    return next;
  };

  const addTask = useCallback((taskEntry, onCompleteCallback) => {
    const next = _mutateStored((cur) => [
      ...cur.filter((t) => t.task_id !== taskEntry.task_id),
      taskEntry,
    ]);
    doneRef.current.delete(taskEntry.task_id);
    setTasks(next);
    if (onCompleteCallback) {
      callbacksRef.current[taskEntry.task_id] = onCompleteCallback;
    }
  }, []);

  const removeTask = useCallback((taskId) => {
    delete callbacksRef.current[taskId];
    doneRef.current.delete(taskId);
    const next = _mutateStored((cur) => cur.filter((t) => t.task_id !== taskId));
    setTasks(next);
    setTaskStatuses((prev) => {
      const { [taskId]: _, ...rest } = prev;
      return rest;
    });
  }, []);

  // 多分頁同步：其他分頁改了 localStorage → 同步本分頁的 tasks 清單
  useEffect(() => {
    const onStorage = (e) => {
      if (e.key === STORAGE_KEY) setTasks(readStored());
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  // Poll active tasks
  useEffect(() => {
    const poll = async () => {
      // 未登入就不打 API —— 否則所有 request 會 401，孤兒清理也走不到。
      if (!useAuthStore.getState().token) return;
      // 分頁在背景時暫停輪詢
      if (typeof document !== "undefined" && document.visibilityState === "hidden") return;

      const stored = readStored();
      // 已終結的任務不再輪詢（狀態保留在 taskStatuses 供 banner 顯示）
      const activeTasks = stored.filter((t) => !doneRef.current.has(t.task_id));
      if (!activeTasks.length) return;

      const results = await Promise.allSettled(
        activeTasks.map((t) => apiClient.get(`tasks/${t.task_id}`))
      );

      const newStatuses = {};
      const orphanTaskIds = [];
      results.forEach((result, idx) => {
        const entry = activeTasks[idx];
        if (result.status === "fulfilled") {
          const data = result.value.data;
          newStatuses[entry.task_id] = data;

          // 觸發 onComplete callback（completed 或 failed 時各呼叫一次）
          const isDone = data.status === "completed" || data.status === "failed";
          if (isDone) {
            doneRef.current.add(entry.task_id);
            const cb = callbacksRef.current[entry.task_id];
            if (cb) {
              cb(data);
              delete callbacksRef.current[entry.task_id];
            }
          }
        } else {
          // server 回 404 / 401 / 403 → 該任務已不存在或當前使用者沒權限看，
          // 一律視為孤兒並從 localStorage 清掉。
          // 5xx / 網路錯誤不在這條清，避免後端短暫掛掉就丟失追蹤。
          const httpStatus = result.reason?.response?.status;
          if (httpStatus === 404 || httpStatus === 401 || httpStatus === 403) {
            orphanTaskIds.push(entry.task_id);
          }
        }
      });

      setTaskStatuses((prev) => ({ ...prev, ...newStatuses }));

      if (orphanTaskIds.length) {
        const orphanSet = new Set(orphanTaskIds);
        orphanTaskIds.forEach((tid) => {
          delete callbacksRef.current[tid];
          doneRef.current.delete(tid);
        });
        const next = _mutateStored((cur) => cur.filter((t) => !orphanSet.has(t.task_id)));
        setTasks(next);
        setTaskStatuses((prev) => {
          const nextS = { ...prev };
          orphanTaskIds.forEach((tid) => delete nextS[tid]);
          return nextS;
        });
      }
    };

    const timer = setInterval(poll, POLL_INTERVAL);
    poll(); // immediate first poll
    return () => clearInterval(timer);
  }, []);

  return (
    <TaskStatusContext.Provider value={{ tasks, taskStatuses, addTask, removeTask }}>
      {children}
    </TaskStatusContext.Provider>
  );
};

// eslint-disable-next-line react-refresh/only-export-components
export const useTaskStatus = () => {
  const ctx = useContext(TaskStatusContext);
  if (!ctx) throw new Error("useTaskStatus must be used within TaskStatusProvider");
  return ctx;
};
