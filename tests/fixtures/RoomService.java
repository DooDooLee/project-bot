package com.example.demo.room;

import java.util.List;

public interface RoomService {
    Room findRoom(Long id);
    Room createRoom(String name, Long userId);
    void leave(Long id);
}
