package com.example.demo.room;

import java.util.List;
import java.util.Optional;

public class RoomServiceImpl implements RoomService {
    private final RoomRepository roomRepository;

    public RoomServiceImpl(RoomRepository roomRepository) {
        this.roomRepository = roomRepository;
    }

    @Override
    public Room findRoom(Long id) {
        return roomRepository.findById(id).orElseThrow(() -> new IllegalStateException("no room"));
    }

    @Override
    public Room createRoom(String name, Long userId) {
        Room room = new Room(name);
        validate(name);
        return roomRepository.save(room);
    }

    @Override
    public void leave(Long id) {
        Room room = findRoom(id);
        room.close();
    }

    private void validate(String name) {}

    static class Helper {
        void help(List<String> names, String... rest) {}
    }
}
