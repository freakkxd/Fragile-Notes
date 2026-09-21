use std::collections::HashSet;
use std::net::TcpListener;

pub struct PortAllocator {
    base: u16,
    max: u16,
}

impl PortAllocator {
    pub fn new(base: u16) -> Self { Self { base, max: base + 20 } }

    // Reserve a port by binding TcpListener briefly, then close and return port.
    // Caller should spawn quickly and verify.
    pub fn allocate(&self, occupied: &HashSet<u16>) -> Result<u16, String> {
        for port in self.base..=self.max {
            if occupied.contains(&port) {
                continue;
            }
            if is_port_free(port) {
                // Try to bind to reserve
                match TcpListener::bind(format!("127.0.0.1:{}", port)) {
                    Ok(listener) => {
                        drop(listener);
                        return Ok(port);
                    }
                    Err(_) => continue,
                }
            }
        }
        Err(format!("no free port in range {}-{}", self.base, self.max))
    }

    pub fn is_free(port: u16) -> bool { is_port_free(port) }
}

fn is_port_free(port: u16) -> bool {
    TcpListener::bind(format!("127.0.0.1:{}", port)).is_ok()
}

pub fn is_port_occupied_external(port: u16) -> bool {
    !is_port_free(port)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;

    #[test]
    fn allocates_8010() {
        let alloc = PortAllocator::new(18010);
        let port = alloc.allocate(&HashSet::new()).unwrap();
        assert!(port >= 18010 && port <= 18030);
    }

    #[test]
    fn release_and_reallocate() {
        let alloc = PortAllocator::new(18020);
        let mut occupied = HashSet::new();
        let p1 = alloc.allocate(&occupied).unwrap();
        occupied.insert(p1);
        // release
        occupied.remove(&p1);
        let p2 = alloc.allocate(&occupied).unwrap();
        assert_eq!(p1, p2);
    }

    #[test]
    fn skips_occupied_external() {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        // port is occupied by listener
        assert!(!PortAllocator::is_free(port));
        drop(listener);
        // after drop, should be free
        assert!(PortAllocator::is_free(port));
    }

    #[test]
    fn parallel_no_collision() {
        let alloc = PortAllocator::new(18040);
        let mut set = HashSet::new();
        for _ in 0..5 {
            let p = alloc.allocate(&set).unwrap();
            assert!(!set.contains(&p));
            set.insert(p);
        }
        assert_eq!(set.len(), 5);
    }
}
