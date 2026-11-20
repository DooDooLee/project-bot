import { useEffect, useState } from "react";
import { getRoom, roomApi } from "../api/room";
import * as rooms from "../api/room";
import RoomCard from "@/components/RoomCard";

const formatName = (name) => name.trim();

export default function RoomPage({ roomId }) {
  const [room, setRoom] = useState(null);

  const loadRoom = async () => {
    const data = await getRoom(roomId);
    setRoom({ ...data, name: formatName(data.name) });
  };

  useEffect(() => {
    loadRoom();
  }, [roomId]);

  const onLeave = () => roomApi.leave(roomId);
  const onCreate = () => rooms.createRoom("new");

  return (
    <ul onClick={onLeave} onDoubleClick={onCreate}>
      {room && <RoomCard room={room} />}
    </ul>
  );
}
