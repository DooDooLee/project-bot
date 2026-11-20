package com.example.demo.room;

import com.example.demo.room.dto.RoomResponse;
import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/v1/rooms")
public class RoomController {
    private static final String DETAIL = "/{roomId}";
    private final RoomService roomService;

    public RoomController(RoomService roomService) {
        this.roomService = roomService;
    }

    /** 방 하나를 조회한다. */
    @GetMapping(DETAIL)
    public RoomResponse getRoom(@PathVariable Long roomId) {
        Room room = roomService.findRoom(roomId);
        return RoomResponse.from(room);
    }

    @PostMapping
    public RoomResponse create(@RequestBody RoomRequest request, Long userId) {
        return RoomResponse.from(this.roomService.createRoom(request.name(), userId));
    }

    @RequestMapping(value = {"/{roomId}/leave", "/{roomId}/exit"}, method = RequestMethod.DELETE)
    public void leave(@PathVariable Long roomId) {
        roomService.leave(roomId);
        log();
    }

    private void log() {}
}
