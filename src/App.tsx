import { useEffect, useMemo, useRef, useState } from "react";
import { invoke } from "@tauri-apps/api/core";
import "./App.css";

type VehicleStatus = "MOVING" | "STATIONARY" | "OVER_LIMIT";

type VehicleId = string | number;

type Vehicle = {
  id: VehicleId;
  plate: string;
  timer: string;
  status: VehicleStatus;
};

type BoundingBox = {
  id: VehicleId;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  status: string;
};

type VehicleHistory = {
  vehicle_id: VehicleId;
  plate?: string | null;
  first_seen?: string | null;
  last_seen?: string | null;
  duration_seconds?: number | null;
  status?: string | null;
  camera_id?: string;
};

type CameraSourceType = "rtsp" | "hls" | "url" | "onvif";

type Camera = {
  id: string;
  name: string;
  source_type: CameraSourceType;
  enabled: boolean;
  zones: number[][][];
  parking_limit_seconds: number;
  credentials_configured: boolean;
  status?: string;
  fps?: number;
  last_error?: string | null;
};

type CurrentUser = { id: string; username: string; role: "ADMIN" | "OPERATOR" | "VIEWER" };

type RemoteAccessStatus = {
  enabled: boolean;
  connected: boolean;
  instance_id: string;
  label: string;
  gateway_url: string;
  tunnel_token_preview: string;
  last_connected_at: string | null;
  last_error: string | null;
};

function normalizeStatus(rawStatus: string): VehicleStatus {
  const value = rawStatus?.toUpperCase();

  if (value === "OVER_LIMIT") return "OVER_LIMIT";
  if (value === "STATIONARY") return "STATIONARY";
  return "MOVING";
}

function formatDuration(seconds: number | null | undefined) {
  const total = Math.max(0, Math.floor(seconds || 0));
  return `${String(Math.floor(total / 60)).padStart(2, "0")}:${String(total % 60).padStart(2, "0")}`;
}

function formatTimestamp(value?: string | null) {
  if (!value) return "N/A";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "N/A" : date.toLocaleString("ro-RO");
}

// FastAPI returns `detail` as a plain string for HTTPException, but as an array of
// Pydantic error objects ({type, loc, msg, input, ctx}) for 422 validation errors.
// Stringifying either of those directly (e.g. `new Error(detail)`) collapses objects
// to "[object Object]"; this extracts the actual human-readable message(s) instead.
function formatErrorDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => {
        if (item && typeof item === "object") {
          const record = item as Record<string, unknown>;
          const msg = typeof record.msg === "string" ? record.msg : typeof record.message === "string" ? record.message : JSON.stringify(record);
          const loc = Array.isArray(record.loc) ? record.loc.join(".") : undefined;
          return loc ? `${loc}: ${msg}` : msg;
        }
        return String(item);
      })
      .join("; ");
  }
  if (detail && typeof detail === "object") return JSON.stringify(detail);
  return String(detail);
}

// Builds a readable "HTTP <status> — code=<code> — <message/detail>" string instead of
// letting a JSON error body collapse into "[object Object]" via a bare `new Error(...)`.
function describeApiError(status: number, payload: unknown, rawBody: string): string {
  const parts: string[] = [`HTTP ${status}`];
  if (payload && typeof payload === "object") {
    const record = payload as Record<string, unknown>;
    if (typeof record.code === "string") parts.push(`code=${record.code}`);
    if (typeof record.message === "string") parts.push(record.message);
    if (record.detail !== undefined) parts.push(formatErrorDetail(record.detail));
  }
  if (parts.length === 1) {
    parts.push(rawBody ? rawBody.slice(0, 500) : "No further details were returned by the server.");
  }
  return parts.join(" — ");
}

let tokenRefreshInFlight: Promise<{ access_token: string; refresh_token: string } | null> | null = null;
let tokenRefreshCache: { previous: string; tokens: { access_token: string; refresh_token: string } } | null = null;

async function apiFetch(
  baseUrl: string,
  token: string,
  path: string,
  init: RequestInit = {},
  refreshToken?: string,
  onRefresh?: (tokens: { access_token: string; refresh_token: string }) => void,
) {
  const send = (accessToken: string) => {
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${accessToken}`);
    if (init.body && !headers.has("Content-Type")) {
      headers.set("Content-Type", "application/json");
    }
    return fetch(`${baseUrl}/api/v1${path}`, { ...init, headers, cache: "no-store" });
  };
  let response = await send(token);
  if (response.status === 401 && refreshToken) {
    let tokens = tokenRefreshCache?.previous === refreshToken ? tokenRefreshCache.tokens : null;
    if (!tokens) {
      if (!tokenRefreshInFlight) {
        const previousRefresh = refreshToken;
        tokenRefreshInFlight = fetch(`${baseUrl}/api/v1/auth/refresh`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ refresh_token: refreshToken }),
          cache: "no-store",
        }).then(async (refreshResponse) => {
          if (!refreshResponse.ok) return null;
          const refreshed = await refreshResponse.json() as { access_token: string; refresh_token: string };
          tokenRefreshCache = { previous: previousRefresh, tokens: refreshed };
          return refreshed;
        }).finally(() => {
          tokenRefreshInFlight = null;
        });
      }
      tokens = await tokenRefreshInFlight;
    }
    if (tokens) {
      onRefresh?.(tokens);
      response = await send(tokens.access_token);
    }
  }
  if (!response.ok) {
    const status = response.status;
    let rawBody = "";
    let payload: unknown = null;
    try {
      rawBody = await response.text();
      payload = rawBody ? JSON.parse(rawBody) : null;
    } catch {
      // Response body was not valid JSON; describeApiError falls back to rawBody.
    }
    throw new Error(describeApiError(status, payload, rawBody));
  }
  return response;
}

type FloatingPanel = "camera" | "history" | "overLimit" | "remoteAccess";

function clamp(value: number, min: number, max: number) {
  return Math.min(Math.max(value, min), max);
}

function useDraggable(initialX: number, initialY: number, width: number, height: number) {
  const [position, setPosition] = useState({ x: initialX, y: initialY });
  const dragState = useRef<{ startX: number; startY: number; originX: number; originY: number } | null>(null);
  const panelRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const handleMouseMove = (event: MouseEvent) => {
      if (!dragState.current) {
        return;
      }

      const viewportWidth = window.innerWidth || document.documentElement.clientWidth || document.body.clientWidth || 0;
      const viewportHeight = window.innerHeight || document.documentElement.clientHeight || document.body.clientHeight || 0;
      const rect = panelRef.current?.getBoundingClientRect();
      const actualWidth = rect?.width || width;
      const actualHeight = rect?.height || height;
      const margin = 12;

      const maxX = Math.max(margin, viewportWidth - actualWidth - margin);
      const maxY = Math.max(margin, viewportHeight - actualHeight - margin);

      const nextX = clamp(dragState.current.originX + (event.clientX - dragState.current.startX), margin, maxX);
      const nextY = clamp(dragState.current.originY + (event.clientY - dragState.current.startY), margin, maxY);

      setPosition({ x: nextX, y: nextY });
    };

    const handleMouseUp = () => {
      dragState.current = null;
    };

    window.addEventListener("mousemove", handleMouseMove);
    window.addEventListener("mouseup", handleMouseUp);

    return () => {
      window.removeEventListener("mousemove", handleMouseMove);
      window.removeEventListener("mouseup", handleMouseUp);
    };
  }, [height, width]);

  const handleMouseDown = (event: React.MouseEvent<HTMLDivElement>) => {
    const interactiveTarget = (event.target as HTMLElement).closest("button, input, textarea, select, a");
    if (interactiveTarget) {
      return;
    }

    dragState.current = {
      startX: event.clientX,
      startY: event.clientY,
      originX: position.x,
      originY: position.y,
    };
    event.preventDefault();
  };

  return { position, handleMouseDown, panelRef };
}

function App() {
  const [vehicles, setVehicles] = useState<Vehicle[]>([]);
  const [aiRunning, setAiRunning] = useState(false);
  const [cameraStatus, setCameraStatus] = useState("CONECTARE...");
  const [livePreviewUrl, setLivePreviewUrl] = useState<string>("");
  const [apiBaseUrl, setApiBaseUrl] = useState<string>("");
  const [apiError, setApiError] = useState("");
  const [accessToken, setAccessToken] = useState("");
  const [refreshToken, setRefreshToken] = useState("");
  const [currentUser, setCurrentUser] = useState<CurrentUser | null>(null);
  const [loginUsername, setLoginUsername] = useState("admin");
  const [loginPassword, setLoginPassword] = useState("");
  const [boundingBoxes, setBoundingBoxes] = useState<BoundingBox[]>([]);
  const [selectedVehicleId, setSelectedVehicleId] = useState<VehicleId | null>(null);
  const [historyByVehicleId, setHistoryByVehicleId] = useState<Record<string, VehicleHistory>>({});
  const [historyVisible, setHistoryVisible] = useState(false);
  const [historyMessage, setHistoryMessage] = useState("");
  const [overLimitVisible, setOverLimitVisible] = useState(false);
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [floatingZIndex, setFloatingZIndex] = useState<Record<FloatingPanel, number>>({
    camera: 20,
    history: 21,
    overLimit: 22,
    remoteAccess: 23,
  });
  const historyDrag = useDraggable(380, 120, 460, 500);
  const overLimitDrag = useDraggable(180, 160, 860, 520);
  const cameraDrag = useDraggable(220, 110, 620, 600);
  const remoteAccessDrag = useDraggable(260, 130, 480, 560);
  const [selectedCameraId, setSelectedCameraId] = useState("");
  const [cameraModalVisible, setCameraModalVisible] = useState(false);
  const [isAddingCamera, setIsAddingCamera] = useState(false);
  const [newCameraSourceType, setNewCameraSourceType] = useState<CameraSourceType>("rtsp");
  const [newCameraId, setNewCameraId] = useState("");
  const [newCameraName, setNewCameraName] = useState("");
  const [newCameraUrl, setNewCameraUrl] = useState("");
  const [newCameraHost, setNewCameraHost] = useState("");
  const [newCameraPort, setNewCameraPort] = useState("80");
  const [newCameraProfile, setNewCameraProfile] = useState("");
  const [newCameraUsername, setNewCameraUsername] = useState("");
  const [newCameraPassword, setNewCameraPassword] = useState("");
  const [newCameraZones, setNewCameraZones] = useState("[]");
  const [newCameraParkingLimit, setNewCameraParkingLimit] = useState("900");
  const [newCameraActive, setNewCameraActive] = useState(true);
  const [cameraTestResult, setCameraTestResult] = useState("");
  const [remoteAccessModalVisible, setRemoteAccessModalVisible] = useState(false);
  const [remoteAccessStatus, setRemoteAccessStatus] = useState<RemoteAccessStatus | null>(null);
  const [remoteAccessGatewayInput, setRemoteAccessGatewayInput] = useState("");
  const [remoteAccessLabelInput, setRemoteAccessLabelInput] = useState("");
  const [remoteAccessEnabledInput, setRemoteAccessEnabledInput] = useState(false);
  const [remoteAccessMessage, setRemoteAccessMessage] = useState("");
  const [remoteAccessBusy, setRemoteAccessBusy] = useState(false);

  useEffect(() => {
    let cancelled = false;
    invoke<string>("ensure_api")
      .then((baseUrl) => {
        if (!cancelled) setApiBaseUrl(baseUrl);
      })
      .catch((error: unknown) => {
        if (!cancelled) setApiError(`API indisponibil: ${String(error)}`);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!accessToken || !apiBaseUrl) return;
    let cancelled = false;
    let timer = 0;
    let lastObjectUrl = "";

    const refreshDashboard = async () => {
      try {
        const query = selectedCameraId ? `?camera_id=${encodeURIComponent(selectedCameraId)}` : "";
        const [dashboardResponse, cameraResponse, historyResponse] = await Promise.all([
          apiFetch(apiBaseUrl, accessToken, `/dashboard${query}`, {}, refreshToken, (tokens) => {
            setAccessToken(tokens.access_token);
            setRefreshToken(tokens.refresh_token);
          }),
          apiFetch(apiBaseUrl, accessToken, "/cameras", {}, refreshToken, (tokens) => {
            setAccessToken(tokens.access_token);
            setRefreshToken(tokens.refresh_token);
          }),
          apiFetch(apiBaseUrl, accessToken, `/history${query}`, {}, refreshToken, (tokens) => {
            setAccessToken(tokens.access_token);
            setRefreshToken(tokens.refresh_token);
          }),
        ]);
        const dashboard = await dashboardResponse.json() as {
          ai_running: boolean;
          cameras: Array<{ camera_id: string; name: string; status: string; fps: number; last_error: string | null; connected: boolean }>;
          vehicles: Array<{ vehicle_id: string; plate: string | null; duration_seconds: number; status: string }>;
          detections: Array<{ vehicle_id: string; payload: { status?: string; box?: BoundingBox }; observed_at: string }>;
        };
        const storedCameras = await cameraResponse.json() as Camera[];
        const history = await historyResponse.json() as VehicleHistory[];
        if (cancelled) return;

        const statusMap = Object.fromEntries(dashboard.cameras.map((camera) => [camera.camera_id, camera]));
        setCameras(storedCameras.map((camera) => ({
          ...camera,
          status: statusMap[camera.id]?.status || (camera.enabled ? "OFFLINE" : "STOPPED"),
          fps: statusMap[camera.id]?.fps || 0,
          last_error: statusMap[camera.id]?.last_error || null,
        })));
        if (!selectedCameraId && storedCameras.length > 0) {
          setSelectedCameraId(storedCameras[0].id);
        }
        setAiRunning(dashboard.ai_running);
        const selectedStatus = selectedCameraId ? statusMap[selectedCameraId] : undefined;
        setCameraStatus(selectedStatus?.status || "OFFLINE");
        setVehicles(dashboard.vehicles.map((item) => ({
          id: item.vehicle_id,
          plate: item.plate || "N/A",
          timer: formatDuration(item.duration_seconds),
          status: normalizeStatus(item.status),
        })));
        setBoundingBoxes(dashboard.detections.flatMap((item) => {
          const box = item.payload?.box;
          return box ? [{ ...box, id: item.vehicle_id, status: item.payload.status || "MOVING" }] : [];
        }));
        setHistoryByVehicleId(Object.fromEntries(history.map((item) => [String(item.vehicle_id), item])));
        setApiError("");
      } catch (error) {
        if (!cancelled) {
          setCameraStatus("OFFLINE");
          setApiError(String(error));
          if (String(error).includes("401")) setAccessToken("");
        }
      } finally {
        if (!cancelled) timer = window.setTimeout(refreshDashboard, 1200);
      }
    };

    void refreshDashboard();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      if (lastObjectUrl) URL.revokeObjectURL(lastObjectUrl);
    };
  }, [accessToken, apiBaseUrl, refreshToken, selectedCameraId]);

  // Live preview is polled back-to-back (not on the slower 1200ms dashboard timer) so the
  // displayed frame rate tracks the backend's real processing fps instead of being capped at ~0.8 fps.
  useEffect(() => {
    if (!accessToken || !apiBaseUrl || !selectedCameraId || cameraStatus !== "ONLINE") {
      setLivePreviewUrl("");
      return;
    }
    let cancelled = false;
    let timer = 0;
    let lastObjectUrl = "";

    const fetchNextFrame = async () => {
      let delayMs = 0;
      try {
        const imageResponse = await apiFetch(
          apiBaseUrl,
          accessToken,
          `/cameras/${encodeURIComponent(selectedCameraId)}/live`,
          {},
          refreshToken,
          (tokens) => {
            setAccessToken(tokens.access_token);
            setRefreshToken(tokens.refresh_token);
          },
        );
        const objectUrl = URL.createObjectURL(await imageResponse.blob());
        if (!cancelled) {
          if (lastObjectUrl) URL.revokeObjectURL(lastObjectUrl);
          lastObjectUrl = objectUrl;
          setLivePreviewUrl(objectUrl);
        } else {
          URL.revokeObjectURL(objectUrl);
        }
      } catch {
        delayMs = 500;
      } finally {
        if (!cancelled) timer = window.setTimeout(fetchNextFrame, delayMs);
      }
    };

    void fetchNextFrame();
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
      if (lastObjectUrl) URL.revokeObjectURL(lastObjectUrl);
    };
  }, [accessToken, apiBaseUrl, refreshToken, selectedCameraId, cameraStatus]);

  const selectedHistory = selectedVehicleId !== null ? historyByVehicleId[String(selectedVehicleId)] : undefined;

  const overLimitVehicles = useMemo(
    () => Object.values(historyByVehicleId).filter((entry) => entry.status === "OVER_LIMIT"),
    [historyByVehicleId]
  );

  const focusFloatingPanel = (panel: FloatingPanel) => {
    setFloatingZIndex((previous) => {
      const maxZ = Math.max(...Object.values(previous));
      return { ...previous, [panel]: maxZ + 1 };
    });
  };

  const openVehicleHistory = (vehicleId?: VehicleId | null) => {
    const targetId = vehicleId ?? selectedVehicleId;

    if (targetId == null) {
      setHistoryMessage("Selectează mai întâi un vehicul.");
      setHistoryVisible(true);
      focusFloatingPanel("history");
      return;
    }

    const entry = historyByVehicleId[String(targetId)];
    setSelectedVehicleId(String(targetId));
    setHistoryMessage(entry ? "" : "Nu există date disponibile pentru acest vehicul.");
    setHistoryVisible(true);
    focusFloatingPanel("history");
  };

  const closeHistory = () => {
    setHistoryVisible(false);
    setHistoryMessage("");
  };

  const openOverLimitTable = () => {
    setOverLimitVisible(true);
    focusFloatingPanel("overLimit");
  };

  const closeOverLimitTable = () => {
    setOverLimitVisible(false);
  };

  const openCameraModal = () => {
    setCameraModalVisible(true);
    focusFloatingPanel("camera");
  };

  const closeCameraModal = () => {
    setCameraModalVisible(false);
    setIsAddingCamera(false);
    setNewCameraName("");
    setNewCameraUrl("");
    setCameraTestResult("");
  };

  const handleSelectCamera = (cameraId: string) => {
    setSelectedCameraId(cameraId);
  };

  const handleToggleCameraEnabled = async (camera: Camera) => {
    if (!apiBaseUrl || !accessToken) return;
    try {
      await apiFetch(apiBaseUrl, accessToken, `/cameras/${encodeURIComponent(camera.id)}`, {
        method: "PATCH",
        body: JSON.stringify({ enabled: !camera.enabled }),
      }, refreshToken);
      setCameras((previous) => previous.map((item) => (item.id === camera.id ? { ...item, enabled: !camera.enabled } : item)));
      setApiError("");
    } catch (error) {
      setApiError(String(error));
    }
  };

  const handleDeleteCamera = async (cameraId: string) => {
    if (!apiBaseUrl || !accessToken) return;
    try {
      await apiFetch(apiBaseUrl, accessToken, `/cameras/${encodeURIComponent(cameraId)}`, { method: "DELETE" }, refreshToken);
      setCameras((previous) => previous.filter((camera) => camera.id !== cameraId));
      if (selectedCameraId === cameraId) setSelectedCameraId("");
    } catch (error) {
      setApiError(String(error));
    }
  };

  const cameraPayload = () => {
    const trimmedName = newCameraName.trim();
    const trimmedId = newCameraId.trim();
    if (!trimmedName || !trimmedId) {
      throw new Error("ID-ul și numele camerei sunt obligatorii.");
    }
    let zones: number[][][];
    try {
      zones = JSON.parse(newCameraZones) as number[][][];
      if (!Array.isArray(zones)) throw new Error();
    } catch {
      throw new Error("Zonele trebuie să fie un JSON valid cu poligoane de coordonate [x, y].");
    }
    return {
      id: trimmedId,
      name: trimmedName,
      source_type: newCameraSourceType,
      url: newCameraUrl.trim(),
      host: newCameraHost.trim(),
      port: newCameraSourceType === "onvif" ? Number(newCameraPort) : null,
      profile: newCameraProfile.trim(),
      username: newCameraUsername,
      password: newCameraPassword,
      enabled: newCameraActive,
      zones,
      parking_limit_seconds: Number(newCameraParkingLimit),
    };
  };

  const handleTestCamera = async () => {
    if (!apiBaseUrl || !accessToken) return;
    try {
      const response = await apiFetch(apiBaseUrl, accessToken, "/cameras/test", {
        method: "POST",
        body: JSON.stringify(cameraPayload()),
      }, refreshToken);
      const result = await response.json() as { code: string; message: string };
      setCameraTestResult(`${result.code}: ${result.message}`);
    } catch (error) {
      setCameraTestResult(String(error));
    }
  };

  const handleSaveCamera = async () => {
    if (!apiBaseUrl || !accessToken) return;
    let payload: ReturnType<typeof cameraPayload>;
    try {
      payload = cameraPayload();
    } catch (error) {
      setApiError(String(error));
      return;
    }
    try {
      await apiFetch(apiBaseUrl, accessToken, "/cameras", {
        method: "POST",
        body: JSON.stringify(payload),
      }, refreshToken);
      setSelectedCameraId(payload.id);
      setApiError("");
      setNewCameraId("");
      setNewCameraName("");
      setNewCameraUrl("");
      setNewCameraHost("");
      setNewCameraProfile("");
      setNewCameraUsername("");
      setNewCameraPassword("");
      setNewCameraZones("[]");
      setNewCameraActive(true);
      setNewCameraSourceType("rtsp");
      setIsAddingCamera(false);
      setCameraTestResult("");
    } catch (error) {
      setApiError(String(error));
    }
  };

  const fetchRemoteAccessStatus = async () => {
    if (!apiBaseUrl || !accessToken) return;
    try {
      const response = await apiFetch(apiBaseUrl, accessToken, "/remote-access/status", {}, refreshToken);
      const status = await response.json() as RemoteAccessStatus;
      setRemoteAccessStatus(status);
      setRemoteAccessGatewayInput(status.gateway_url);
      setRemoteAccessLabelInput(status.label);
      setRemoteAccessEnabledInput(status.enabled);
    } catch (error) {
      setRemoteAccessMessage(String(error));
    }
  };

  const openRemoteAccessModal = () => {
    setRemoteAccessModalVisible(true);
    setRemoteAccessMessage("");
    focusFloatingPanel("remoteAccess");
    void fetchRemoteAccessStatus();
  };

  const closeRemoteAccessModal = () => {
    setRemoteAccessModalVisible(false);
  };

  const handleSaveRemoteAccessConfig = async () => {
    if (!apiBaseUrl || !accessToken) return;
    setRemoteAccessBusy(true);
    setRemoteAccessMessage("");
    try {
      const response = await apiFetch(apiBaseUrl, accessToken, "/remote-access/config", {
        method: "POST",
        body: JSON.stringify({
          gateway_url: remoteAccessGatewayInput.trim(),
          label: remoteAccessLabelInput.trim(),
          enabled: remoteAccessEnabledInput,
        }),
      }, refreshToken);
      const status = await response.json() as RemoteAccessStatus;
      setRemoteAccessStatus(status);
      setRemoteAccessMessage("Configurație salvată.");
    } catch (error) {
      setRemoteAccessMessage(String(error));
    } finally {
      setRemoteAccessBusy(false);
    }
  };

  const handleTestRemoteAccessConnection = async () => {
    if (!apiBaseUrl || !accessToken) return;
    setRemoteAccessBusy(true);
    setRemoteAccessMessage("Se testează conexiunea...");
    try {
      const response = await apiFetch(apiBaseUrl, accessToken, "/remote-access/test", {
        method: "POST",
        body: JSON.stringify({ gateway_url: remoteAccessGatewayInput.trim() }),
      }, refreshToken);
      const result = await response.json() as { ok: boolean; detail: string };
      setRemoteAccessMessage(result.ok ? `OK: ${result.detail}` : `Eșec: ${result.detail}`);
    } catch (error) {
      setRemoteAccessMessage(String(error));
    } finally {
      setRemoteAccessBusy(false);
    }
  };

  const handleRegenerateRemoteAccessCredentials = async () => {
    if (!apiBaseUrl || !accessToken) return;
    if (!window.confirm("Regenerarea invalidează imediat tunelul curent. Continuați?")) return;
    setRemoteAccessBusy(true);
    setRemoteAccessMessage("");
    try {
      const response = await apiFetch(apiBaseUrl, accessToken, "/remote-access/regenerate-credentials", {
        method: "POST",
      }, refreshToken);
      const status = await response.json() as RemoteAccessStatus;
      setRemoteAccessStatus(status);
      setRemoteAccessMessage("Credențiale regenerate.");
    } catch (error) {
      setRemoteAccessMessage(String(error));
    } finally {
      setRemoteAccessBusy(false);
    }
  };

  const handleLogin = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!apiBaseUrl) return;
    try {
      const response = await fetch(`${apiBaseUrl}/api/v1/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: loginUsername, password: loginPassword }),
      });
      if (!response.ok) {
        throw new Error(response.status === 429 ? "Prea multe încercări. Așteaptă un minut." : "Utilizator sau parolă incorectă.");
      }
      const result = await response.json() as { access_token: string; refresh_token: string; user: CurrentUser };
      setAccessToken(result.access_token);
      setRefreshToken(result.refresh_token);
      setCurrentUser(result.user);
      setLoginPassword("");
      setApiError("");
    } catch (error) {
      setApiError(String(error));
    }
  };

  const handleLogout = () => {
    if (apiBaseUrl && accessToken && refreshToken) {
      void apiFetch(apiBaseUrl, accessToken, "/auth/logout", {
        method: "POST",
        body: JSON.stringify({ refresh_token: refreshToken }),
      }, refreshToken).catch(() => {});
    }
    setAccessToken("");
    setRefreshToken("");
    setCurrentUser(null);
    setVehicles([]);
    setCameras([]);
    setSelectedCameraId("");
    setLivePreviewUrl("");
  };

  const handlePreviewClick = (event: React.MouseEvent<HTMLImageElement>) => {
    if (!boundingBoxes.length) {
      return;
    }

    const imageElement = event.currentTarget;
    const rect = imageElement.getBoundingClientRect();
    const naturalWidth = imageElement.naturalWidth || 1280;
    const naturalHeight = imageElement.naturalHeight || 720;

    const displayWidth = rect.width || 1;
    const displayHeight = rect.height || 1;
    const objectFit = imageElement.style.objectFit || "cover";

    let scale = 1;
    let offsetX = 0;
    let offsetY = 0;

    if (objectFit === "contain") {
      scale = Math.min(displayWidth / naturalWidth, displayHeight / naturalHeight);
      offsetX = (displayWidth - naturalWidth * scale) / 2;
      offsetY = (displayHeight - naturalHeight * scale) / 2;
    } else {
      scale = Math.max(displayWidth / naturalWidth, displayHeight / naturalHeight);
      const visibleWidth = naturalWidth * scale;
      const visibleHeight = naturalHeight * scale;
      offsetX = (displayWidth - visibleWidth) / 2;
      offsetY = (displayHeight - visibleHeight) / 2;
    }

    const relativeX = (event.clientX - rect.left - offsetX) / Math.max(scale, 0.0001);
    const relativeY = (event.clientY - rect.top - offsetY) / Math.max(scale, 0.0001);

    const clicked = boundingBoxes.find((box) => {
      if (box.status !== "OVER_LIMIT") {
        return false;
      }

      return (
        relativeX >= box.x1 &&
        relativeX <= box.x2 &&
        relativeY >= box.y1 &&
        relativeY <= box.y2
      );
    });

    if (!clicked) {
      return;
    }

    setSelectedVehicleId(String(clicked.id));

    const card = document.getElementById(`vehicle-card-${String(clicked.id)}`);
    if (card) {
      card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  };

  async function handleStartAi() {
    if (!apiBaseUrl || !accessToken) return;
    try {
      await apiFetch(apiBaseUrl, accessToken, "/system/start", { method: "POST" }, refreshToken);
      setAiRunning(true);
      setApiError("");
    } catch (error) {
      setApiError(String(error));
    }
  }

  async function handleStopAi() {
    if (!apiBaseUrl || !accessToken) return;
    try {
      await apiFetch(apiBaseUrl, accessToken, "/system/stop", { method: "POST" }, refreshToken);
      setAiRunning(false);
      setApiError("");
    } catch (error) {
      setApiError(String(error));
    }
  }

  if (!accessToken) {
    return (
      <div className="parking-app">
        <header className="topbar">
          <div>
            <h1>ParkingAI</h1>
            <p>Autentificare securizată la serviciul ParkingAI</p>
          </div>
        </header>
        <form className="login-card" onSubmit={handleLogin}>
          <label>
            Utilizator
            <input value={loginUsername} onChange={(event) => setLoginUsername(event.target.value)} autoComplete="username" />
          </label>
          <label>
            Parolă
            <input
              type="password"
              value={loginPassword}
              onChange={(event) => setLoginPassword(event.target.value)}
              autoComplete="current-password"
            />
          </label>
          <button className="start-ai-button" type="submit" disabled={!apiBaseUrl}>
            {apiBaseUrl ? "Autentificare" : "Pornire serviciu API..."}
          </button>
          {apiError && <p className="api-error">{apiError}</p>}
        </form>
      </div>
    );
  }

  return (
    <div className="parking-app">
      <header className="topbar">
        <div>
          <h1>ParkingAI</h1>
          <p>Monitorizare inteligentă a parcării</p>
        </div>
        <button type="button" className="secondary-button" onClick={handleLogout}>Deconectare</button>
      </header>

      {historyVisible && (
        <div
          className="floating-window history-modal"
          style={{
            left: historyDrag.position.x,
            top: historyDrag.position.y,
            width: "min(92vw, 460px)",
            zIndex: floatingZIndex.history,
          }}
          onMouseDown={() => focusFloatingPanel("history")}
          onClick={(event) => event.stopPropagation()}
        >
          <div className="history-header drag-handle" onMouseDown={historyDrag.handleMouseDown}>
            <h3>Istoric vehicul</h3>
            <button type="button" className="history-close" onClick={closeHistory}>Închide</button>
          </div>

          {historyMessage ? (
            <div className="history-message">{historyMessage}</div>
          ) : selectedHistory ? (
            <div className="history-grid">
              <div className="history-item"><span>ID vehicul</span><strong>{selectedHistory.vehicle_id}</strong></div>
              <div className="history-item"><span>Număr înmatriculare</span><strong>{selectedHistory.plate || "N/A"}</strong></div>
              <div className="history-item"><span>Prima observare</span><strong>{formatTimestamp(selectedHistory.first_seen)}</strong></div>
              <div className="history-item"><span>Ultima observare</span><strong>{formatTimestamp(selectedHistory.last_seen)}</strong></div>
              <div className="history-item"><span>Durată staționare</span><strong>{formatDuration(selectedHistory.duration_seconds)}</strong></div>
              <div className="history-item"><span>Status</span><strong>{selectedHistory.status || "N/A"}</strong></div>
              <div className="history-item"><span>Cameră</span><strong>{selectedHistory.camera_id || "N/A"}</strong></div>
            </div>
          ) : (
            <div className="history-message">Nu există date disponibile pentru acest vehicul.</div>
          )}
        </div>
      )}

      {overLimitVisible && (
        <div
          ref={overLimitDrag.panelRef}
          className="floating-window history-modal over-limit-modal"
          style={{
            left: overLimitDrag.position.x,
            top: overLimitDrag.position.y,
            width: "min(92vw, 860px)",
            zIndex: floatingZIndex.overLimit,
          }}
          onMouseDown={() => focusFloatingPanel("overLimit")}
          onClick={(event) => event.stopPropagation()}
        >
          <div className="history-header drag-handle" onMouseDown={overLimitDrag.handleMouseDown}>
            <h3>Vehicule depășire limită</h3>
            <button type="button" className="history-close" onClick={closeOverLimitTable}>Închide</button>
          </div>

          <div className="over-limit-table-wrap">
            {overLimitVehicles.length === 0 ? (
              <div className="history-message">Nu există vehicule în stare de depășire limită.</div>
            ) : (
              <table className="over-limit-table">
                <thead>
                  <tr>
                    <th>ID</th>
                    <th>Număr</th>
                    <th>Prima observare</th>
                    <th>Ultima observare</th>
                    <th>Durată</th>
                    <th>Cameră</th>
                    <th>Status</th>
                  </tr>
                </thead>
                <tbody>
                  {overLimitVehicles.map((entry) => (
                    <tr
                      key={entry.vehicle_id}
                      onClick={() => {
                        setSelectedVehicleId(entry.vehicle_id);
                        setOverLimitVisible(false);
                        setHistoryMessage("");
                        setHistoryVisible(true);
                      }}
                      className="over-limit-row"
                    >
                      <td>{entry.vehicle_id}</td>
                      <td>{entry.plate || "N/A"}</td>
                      <td>{formatTimestamp(entry.first_seen)}</td>
                      <td>{formatTimestamp(entry.last_seen)}</td>
                      <td>{formatDuration(entry.duration_seconds)}</td>
                      <td>{entry.camera_id || "N/A"}</td>
                      <td>{entry.status || "N/A"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>
      )}

      <main className="content-shell">
        <section className="camera-panel">
          <div className="camera-placeholder">
            <span>{selectedCameraId ? `CAMERA ${selectedCameraId}` : "CAMERA LIVE"}</span>
            {livePreviewUrl ? (
              <img
                src={livePreviewUrl}
                alt="ParkingAI live preview"
                onClick={handlePreviewClick}
                style={{ width: "100%", height: "100%", objectFit: "cover", borderRadius: 12, cursor: "pointer" }}
              />
            ) : (
              <div className="empty-state">{selectedCameraId ? "Flux video indisponibil." : "Adaugă o cameră pentru a începe."}</div>
            )}
            <strong>{vehicles.length > 0 ? "AI conectat" : aiRunning ? "Așteptare detecții..." : "Motor AI oprit"}</strong>
          </div>
        </section>

        <aside className="vehicle-panel">
          <div className="panel-header">
            <h2>Vehicule detectate</h2>
          </div>

          <div className="vehicle-list">
            {vehicles.length === 0 ? (
              <div className="empty-state">Așteptare date AI...</div>
            ) : (
              vehicles.map((vehicle) => (
                <div
                  key={vehicle.id}
                  id={`vehicle-card-${vehicle.id}`}
                  className={`vehicle-card ${
                    vehicle.status === "OVER_LIMIT"
                      ? "overlimit"
                      : vehicle.status === "STATIONARY"
                        ? "stationary"
                        : "moving"
                  } ${selectedVehicleId === vehicle.id ? "selected" : ""}`}
                >
                  <div className="vehicle-row">
                    <span className="vehicle-id">ID {vehicle.id}</span>
                  </div>
                  <div className="vehicle-plate">{vehicle.plate || "N/A"}</div>
                  <div className="vehicle-time">{vehicle.timer}</div>
                  {vehicle.status === "OVER_LIMIT" && (
                    <div className="vehicle-status">DEPĂȘIRE LIMITĂ</div>
                  )}
                </div>
              ))
            )}
          </div>
        </aside>
      </main>

      <footer className="statusbar">
        <div className="status-item">
          <span className="status-label">Sistem:</span>
          <span className="status-value online">ONLINE</span>
        </div>
        <div className="status-item">
          <span className="status-label">Cameră:</span>
          <span className="status-value">{cameraStatus}</span>
        </div>
        <div className="status-item">
          <span className="status-label">AI:</span>
          <span className="status-value">{aiRunning ? "RUNNING" : "PREGĂTIT"}</span>
        </div>
        <div className="status-item">
          <span className="status-label">API:</span>
          <span className="status-value">{apiBaseUrl ? apiBaseUrl : "OFFLINE"}</span>
        </div>

        <div className="secondary-actions">
          {currentUser?.role === "ADMIN" && (
            <button type="button" className="secondary-button camera-button" onClick={openCameraModal}>
              CAMERE
            </button>
          )}
          {currentUser?.role === "ADMIN" && (
            <button type="button" className="secondary-button" onClick={openRemoteAccessModal}>
              ACCES LA DISTANȚĂ
            </button>
          )}
          <button
            type="button"
            className="secondary-button"
            onClick={() => openVehicleHistory(selectedVehicleId)}
          >
            Istoric vehicul
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={openOverLimitTable}
          >
            Vehicule depășire limită
          </button>
        </div>

        <button
          type="button"
          className="start-ai-button"
          onClick={aiRunning ? handleStopAi : handleStartAi}
        >
          {aiRunning ? "Oprește AI" : "Pornește AI"}
        </button>
        {apiError && <span className="api-error">{apiError}</span>}
      </footer>

      {cameraModalVisible && (
        <div
          ref={cameraDrag.panelRef}
          className="floating-window camera-modal"
          style={{
            left: cameraDrag.position.x,
            top: cameraDrag.position.y,
            width: "min(92vw, 620px)",
            zIndex: floatingZIndex.camera,
          }}
          onMouseDown={() => focusFloatingPanel("camera")}
          onClick={(event) => event.stopPropagation()}
        >
          <div className="history-header drag-handle" onMouseDown={cameraDrag.handleMouseDown}>
            <h3>Camere</h3>
            <button type="button" className="history-close" onClick={closeCameraModal}>Închide</button>
          </div>

          <div className="camera-modal-body">
            {!isAddingCamera ? (
              <>
                <div className="camera-action-row">
                  <span>{cameras.length}/20 camere</span>
                  <button
                    type="button"
                    className="camera-action-button"
                    disabled={cameras.length >= 20}
                    onClick={() => {
                      setApiError("");
                      setIsAddingCamera(true);
                    }}
                  >
                    + ADAUGĂ CAMERA
                  </button>
                </div>
                {apiError && <div className="api-error">{apiError}</div>}

                <div className="camera-list">
                  {cameras.map((camera) => (
                    <div
                      key={camera.id}
                      className={`camera-item ${selectedCameraId === camera.id ? "camera-item-selected" : ""}`}
                    >
                          <div className="camera-item-main">
                            <div className="camera-name">{camera.name} <span className="camera-url">({camera.id})</span></div>
                            <div className="camera-url">
                              {camera.last_error || `${camera.fps?.toFixed(1) || "0.0"} FPS · ${camera.zones.length} zone · limită ${camera.parking_limit_seconds}s`}
                            </div>
                          </div>

                          <div className="camera-meta-row">
                            <span className={`camera-pill ${camera.enabled ? "active" : "inactive"}`}>
                              {camera.enabled ? "Activă" : "Inactivă"}
                            </span>
                            <span className={`camera-pill status-${(camera.status || "offline").toLowerCase()}`}>
                              {camera.status || "OFFLINE"}
                            </span>
                            <span className="camera-source-tag">{camera.source_type.toUpperCase()}</span>
                          </div>

                          <div className="camera-item-actions">
                            <button type="button" className="camera-select-button" onClick={() => handleSelectCamera(camera.id)}>
                              {selectedCameraId === camera.id ? "SELECTATĂ" : "SELECTEAZĂ"}
                            </button>
                            <button type="button" className="camera-toggle-button" onClick={() => void handleToggleCameraEnabled(camera)}>
                              {camera.enabled ? "DEZACTIVEAZĂ" : "ACTIVEAZĂ"}
                            </button>
                            <button type="button" className="camera-delete-button" onClick={() => void handleDeleteCamera(camera.id)}>
                              ȘTERGE
                            </button>
                          </div>
                        </div>
                      ))}
                </div>
              </>
            ) : (
              <div className="camera-form">
                <label>
                      ID cameră
                      <input
                        value={newCameraId}
                        onChange={(event) => setNewCameraId(event.target.value)}
                        placeholder="camera-01"
                        autoComplete="off"
                      />
                </label>

                <label>
                      Tip sursă
                      <select
                        value={newCameraSourceType}
                        onChange={(event) => setNewCameraSourceType(event.target.value as CameraSourceType)}
                      >
                        <option value="rtsp">RTSP</option>
                        <option value="onvif">ONVIF</option>
                        <option value="hls">HLS</option>
                        <option value="url">URL generic</option>
                      </select>
                </label>

                <label>
                  Nume cameră
                  <input
                    type="text"
                    value={newCameraName}
                    onChange={(event) => setNewCameraName(event.target.value)}
                    placeholder="Ex: Camera intrare"
                  />
                </label>

                {newCameraSourceType !== "onvif" && (
                  <label>
                    URL stream
                    <input
                      type="text"
                      value={newCameraUrl}
                      onChange={(event) => setNewCameraUrl(event.target.value)}
                      placeholder={newCameraSourceType === "hls" ? "https://host/stream.m3u8" : "rtsp://host:554/stream"}
                      autoComplete="off"
                    />
                  </label>
                )}

                {newCameraSourceType === "onvif" && (
                  <>
                    <label>
                      IP / hostname ONVIF
                      <input
                        type="text"
                        value={newCameraHost}
                        onChange={(event) => setNewCameraHost(event.target.value)}
                        autoComplete="off"
                      />
                    </label>
                    <label>
                      Port ONVIF
                      <input type="number" min="1" max="65535" value={newCameraPort} onChange={(event) => setNewCameraPort(event.target.value)} />
                    </label>
                    <label>
                      Profil / stream (opțional)
                      <input value={newCameraProfile} onChange={(event) => setNewCameraProfile(event.target.value)} autoComplete="off" />
                    </label>
                  </>
                )}

                <label>
                  Username cameră (opțional)
                  <input value={newCameraUsername} onChange={(event) => setNewCameraUsername(event.target.value)} autoComplete="off" />
                </label>
                <label>
                  Parolă cameră (criptată la salvare)
                  <input type="password" value={newCameraPassword} onChange={(event) => setNewCameraPassword(event.target.value)} autoComplete="new-password" />
                </label>
                <label>
                  Zone de parcare (JSON, poligoane în pixeli)
                  <textarea value={newCameraZones} onChange={(event) => setNewCameraZones(event.target.value)} placeholder="[[[0,0],[100,0],[100,100],[0,100]]]" />
                </label>
                <label>
                  Timp maxim de parcare (secunde)
                  <input type="number" min="1" value={newCameraParkingLimit} onChange={(event) => setNewCameraParkingLimit(event.target.value)} />
                </label>
                <label className="camera-toggle-row">
                  <input
                    type="checkbox"
                    checked={newCameraActive}
                    onChange={(event) => setNewCameraActive(event.target.checked)}
                  />
                  <span>Activ</span>
                </label>

                {cameraTestResult && <div className="history-message">{cameraTestResult}</div>}
                <div className="camera-form-actions">
                  <button type="button" className="camera-action-button" onClick={() => void handleTestCamera()}>
                    TESTEAZĂ CONEXIUNEA
                  </button>
                  <button type="button" className="camera-select-button" onClick={handleSaveCamera}>
                    SALVEAZĂ
                  </button>
                  <button
                    type="button"
                    className="camera-delete-button"
                    onClick={() => {
                      setIsAddingCamera(false);
                      setNewCameraId("");
                      setNewCameraName("");
                      setNewCameraUrl("");
                      setNewCameraHost("");
                      setNewCameraProfile("");
                      setNewCameraUsername("");
                      setNewCameraPassword("");
                      setNewCameraZones("[]");
                      setNewCameraActive(true);
                      setNewCameraSourceType("rtsp");
                      setCameraTestResult("");
                    }}
                  >
                    ANULEAZĂ
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {remoteAccessModalVisible && (
        <div
          ref={remoteAccessDrag.panelRef}
          className="floating-window camera-modal"
          style={{
            left: remoteAccessDrag.position.x,
            top: remoteAccessDrag.position.y,
            width: "min(92vw, 480px)",
            zIndex: floatingZIndex.remoteAccess,
          }}
          onMouseDown={() => focusFloatingPanel("remoteAccess")}
          onClick={(event) => event.stopPropagation()}
        >
          <div className="history-header drag-handle" onMouseDown={remoteAccessDrag.handleMouseDown}>
            <h3>Acces la distanță</h3>
            <button type="button" className="history-close" onClick={closeRemoteAccessModal}>Închide</button>
          </div>

          <div className="camera-modal-body">
            <div className="camera-form">
              <div className="camera-meta-row">
                <span className={`camera-pill ${remoteAccessStatus?.connected ? "active" : "inactive"}`}>
                  {remoteAccessStatus?.connected ? "CONECTAT" : "DECONECTAT"}
                </span>
                <span className={`camera-pill ${remoteAccessStatus?.enabled ? "active" : "inactive"}`}>
                  {remoteAccessStatus?.enabled ? "Activat" : "Dezactivat"}
                </span>
              </div>

              <label>
                ID instanță (identificator unic, nu se schimbă cu IP-ul)
                <input type="text" value={remoteAccessStatus?.instance_id || ""} readOnly />
              </label>

              <label>
                Etichetă (ex: Primăria X - Sediu central)
                <input
                  type="text"
                  value={remoteAccessLabelInput}
                  onChange={(event) => setRemoteAccessLabelInput(event.target.value)}
                  placeholder="Etichetă opțională"
                />
              </label>

              <label>
                Adresă gateway/relay (https:// sau wss://)
                <input
                  type="text"
                  value={remoteAccessGatewayInput}
                  onChange={(event) => setRemoteAccessGatewayInput(event.target.value)}
                  placeholder="https://relay.exemplu-primarie.ro"
                  autoComplete="off"
                />
              </label>

              <label className="camera-toggle-row">
                <input
                  type="checkbox"
                  checked={remoteAccessEnabledInput}
                  onChange={(event) => setRemoteAccessEnabledInput(event.target.checked)}
                />
                <span>Activează accesul la distanță</span>
              </label>

              <label>
                Token tunel (afișat parțial, nu este o parolă de utilizator)
                <input type="text" value={remoteAccessStatus?.tunnel_token_preview || ""} readOnly />
              </label>

              <div className="camera-url">
                Ultima conectare: {formatTimestamp(remoteAccessStatus?.last_connected_at)}
                {remoteAccessStatus?.last_error && (
                  <>
                    {" · "}
                    Ultima eroare: {remoteAccessStatus.last_error}
                  </>
                )}
              </div>

              {remoteAccessMessage && <div className="history-message">{remoteAccessMessage}</div>}

              <div className="camera-form-actions">
                <button
                  type="button"
                  className="camera-action-button"
                  disabled={remoteAccessBusy || !remoteAccessGatewayInput.trim()}
                  onClick={() => void handleTestRemoteAccessConnection()}
                >
                  TESTEAZĂ CONEXIUNEA
                </button>
                <button
                  type="button"
                  className="camera-select-button"
                  disabled={remoteAccessBusy}
                  onClick={() => void handleSaveRemoteAccessConfig()}
                >
                  SALVEAZĂ
                </button>
                <button
                  type="button"
                  className="camera-delete-button"
                  disabled={remoteAccessBusy}
                  onClick={() => void handleRegenerateRemoteAccessCredentials()}
                >
                  REGENEREAZĂ CREDENȚIALE
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
