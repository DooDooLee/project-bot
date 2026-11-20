import api from "./api";

const BASE = "/api/v1/rooms";
const baseUrl = () => import.meta.env.VITE_API_BASE_URL;

export async function getRoom(roomId) {
  const { data } = await api.get(`${BASE}/${roomId}`);
  return data;
}

export const createRoom = async (name) => {
  const res = await api.post(BASE, { name });
  return res.data;
};

export const roomApi = {
  leave: (roomId) => api.delete("/api/v1/rooms/" + roomId + "/leave"),
};

export async function streamRoom(roomId) {
  return fetch(`${baseUrl()}${BASE}/${encodeURIComponent(roomId)}/events?since=1`, { method: "POST" });
}
