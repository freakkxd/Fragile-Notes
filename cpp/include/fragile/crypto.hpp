#pragma once
#include <string>
#include <vector>
#include <cstdint>
#include <cstddef>

namespace fragile {

std::vector<uint8_t> encrypt_aes_gcm(const std::vector<uint8_t>& plaintext, const std::vector<uint8_t>& key, const std::vector<uint8_t>& nonce);
std::vector<uint8_t> decrypt_aes_gcm(const std::vector<uint8_t>& ciphertext, const std::vector<uint8_t>& key, const std::vector<uint8_t>& nonce);
std::vector<uint8_t> derive_key_pbkdf2(const std::string& password, const std::vector<uint8_t>& salt, int iterations = 100000);
std::vector<uint8_t> random_bytes(size_t n);

}
