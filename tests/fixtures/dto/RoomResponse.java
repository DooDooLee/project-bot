package com.example.demo.room.dto;

import com.example.demo.room.Room;

public record RoomResponse(Long id, String name) {
    public static RoomResponse from(Room room) {
        return new RoomResponse(1L, "x");
    }
}
