package com.example.demo.room;

import java.util.Optional;

public interface RoomRepository {
    Optional<Room> findById(Long id);
    Room save(Room room);
}
