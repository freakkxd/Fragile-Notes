#[cfg(test)]
mod runtime_tests {
    use super::super::runtime::allocator::PortAllocator;
    use std::collections::HashSet;
    use std::net::TcpListener;

    #[test]
    fn test_port_allocator_no_collision() {
        let alloc = PortAllocator::new(18200);
        let mut set = HashSet::new();
        let p1 = alloc.allocate(&set).unwrap();
        set.insert(p1);
        let p2 = alloc.allocate(&set).unwrap();
        assert_ne!(p1, p2);
    }
}
